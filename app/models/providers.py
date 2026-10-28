"""Provider transport: speaks to the model server.

Moved out of `app/api.py`. `api` re-exports these names because routes and
tests refer to them, and two copies of the NDJSON parser would drift.

The endpoint is the llama.cpp native `/api/chat` route; the OpenAI-shaped
`/v1/...` set is used only for the model list.
"""

import asyncio
import json
import re

import httpx

from ..config import settings


def base_url():
    """The server root, with the OpenAI-style suffix off.

    MODEL_BASE_URL keeps a `/v1` suffix for the model list; every other route
    here — chat, the loaded-model report, the model's own metadata — is on the
    native API, and a path appended to `/v1` would be a 404.
    """
    base = settings.model_base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def chat_url():
    """The model talks on the native /api/chat path, framed as NDJSON."""
    return base_url() + "/api/chat"


def to_native(messages):
    """OpenAI-shaped content parts to the native `messages` + `images` shape."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            text = "".join(p.get("text", "") for p in c if p.get("type") == "text")
            imgs = [p["image_url"]["url"].split(",", 1)[-1] for p in c if p.get("type") == "image_url"]
            mm = {"role": m["role"], "content": text}
            if imgs:
                mm["images"] = imgs
            out.append(mm)
        else:
            out.append(m)
    return out


def normalize_call(call):
    """Native streams send tool arguments as an object, OpenAI-style servers
    send a JSON string. Runners downstream expect the object."""
    fn = call.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
        call = {**call, "function": {**fn, "arguments": args}}
    return call


def parse_metrics(chunk):
    """The counts a finished frame carries, or `None` for any other frame.

    Prompt usage is stated exactly once per call, on the frame that closes the
    stream, and nowhere else — it is not derivable from the text. Every field
    is optional: a server that counts differently reports fewer of them, and a
    missing count is better than a made-up one.
    """
    if not chunk.get("done"):
        return None
    out = {}
    for name in ("prompt_eval_count", "prompt_eval_cached_count", "eval_count",
                 "eval_duration", "prompt_eval_duration", "load_duration",
                 "total_duration"):
        value = chunk.get(name)
        if isinstance(value, (int, float)):
            out[name] = value
    return out or None


def parse_chunk(line):
    """One streamed line -> list of (kind, value) events.

    The native stream is bare JSON with a `message` object, one per line.
    An OpenAI-compatible server answers instead with SSE frames like
    `data: {"choices": [{"delta": {...}}]}`, where thinking arrives as
    `reasoning_content` instead of `thinking`. Blank lines, keep-alives,
    `[DONE]` and partial JSON are skipped rather than raising, so a stray
    frame can never end the turn with an empty answer.
    """
    line = line.strip()
    if not line:
        return []
    if line.startswith("data:"):
        line = line[len("data:") :].strip()
        if line == "[DONE]":
            return []
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        return []
    msg = chunk.get("message")
    if msg is None:
        choices = chunk.get("choices") or []
        msg = (choices[0].get("delta") or {}) if choices else {}
    events = []
    if msg.get("content"):
        events.append(("delta", msg["content"]))
    think = msg.get("thinking") or msg.get("reasoning_content")
    if think:
        events.append(("thinking", think))
    if msg.get("tool_calls"):
        events.append(("tool_calls", [normalize_call(c) for c in msg["tool_calls"]]))
    # The closing frame carries both a last message and the counts, so this is
    # an addition to that frame rather than a branch away from it.
    done = parse_metrics(chunk)
    if done:
        events.append(("metrics", done))
    return events


def merge_tool_calls(calls, incoming):
    """The server streams one tool call per line, each with its own id, so a
    round can carry several. A repeated id replaces the earlier copy instead of
    running the same call twice."""
    for call in incoming:
        for i, seen in enumerate(calls):
            if call.get("id") and seen.get("id") == call["id"]:
                calls[i] = call
                break
        else:
            calls.append(call)


class ModelError(Exception):
    """The model server refused, failed, or could not be reached.

    An unreachable server is the same thing to the runtime as a refused
    request: the turn cannot continue, and the reason has to reach the user as
    a stated error rather than as a traceback. Wrapping httpx here is what
    keeps that promise, because the runtime only catches this type.
    """

    def __init__(self, message, *, status=None, error=None):
        super().__init__(message)
        self.status = status
        self.error = error


def think_for_request(thinking, model, tools):
    """The `think` value a request should actually carry.

    On some templates `false` does not mean "think less": it takes the tool
    list with it, and the model answers from memory — inventing presidents,
    inventing prices — while the search it would have run never happens. So a
    round that offers tools never sends `false`; it sends the lowest level the
    model names, and when it names none the field is dropped and the server's
    own default applies, which is what every request carried before the ladder
    existed. A tool-free round is a different matter: `false` is exactly what
    the final pass wants, and nothing there can be silently disabled.
    """
    if thinking is not False or not tools:
        return thinking
    levels = [v for v in (cached_ladder(model) or []) if isinstance(v, str)]
    return levels[0] if levels else None


async def stream_completion(messages, model=None, thinking=False, tools=None, ctx=None):
    payload = {
        "model": model or settings.model_name,
        "messages": to_native(messages),
        "stream": True,
    }
    # `false` skips the thinking channel, `true` takes the model's own
    # default, and a level names a stated effort. The ladder is per model,
    # so the value arrives already resolved against think_ladder rather
    # than assumed here; `think_for_request` keeps a tool round off the one
    # rung that would also switch the tools off.
    value = think_for_request(thinking, model, tools)
    if value is not None:
        payload["think"] = value
    if ctx:
        payload["options"] = {"num_ctx": ctx}
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {settings.model_api_key}"}
    url = chat_url()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as c:
            async with c.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode(errors="replace")
                    # The server's own text says what is wrong with the request;
                    # the status alone does not.
                    raise ModelError(
                        f"model server returned {r.status_code}: {body[:400]}",
                        status=r.status_code,
                    )
                async for line in r.aiter_lines():
                    for event in parse_chunk(line):
                        yield event
    except httpx.HTTPError as e:
        raise ModelError(f"cannot reach the model server at {chat_url()}: {e}") from e


async def list_models():
    """Model ids the endpoint reports. Empty when it cannot be reached."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                f"{settings.model_base_url}/models",
                headers={"Authorization": f"Bearer {settings.model_api_key}"},
            )
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        return []


# A parameter count that stands on its own: "4B", "0.5B", "70B".
SIZE = re.compile(r"^(\d+(?:\.\d+)?)B$", re.I)


def model_label(mid):
    """A model id as a person would read it.

    The registry path, the GGUF suffix and a `latest` tag all name the file
    rather than the model, so they are dropped: `huggingface.co/XHToken/
    Spark-X2.6-4B-GGUF:latest` becomes `Spark X2.6:4B`. Nothing here changes
    what is stored or sent — the id stays the only handle the endpoint accepts.

    A tag that is not `latest` is kept, because in Ollama it is the variant
    (`:Q4_K_M`, `:14b`) and dropping it would give two entries one name.
    """
    name = mid.rsplit("/", 1)[-1]
    # The tag comes off before the suffix, because a `.gguf` in the tag's
    # place — `...-Q4_K_M.gguf:latest` — is the file the tag was carved from.
    base, _, tag = name.rpartition(":")
    if not base:
        base, tag = name, ""
    for suffix in (".gguf", "-gguf"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break

    words = [w for w in base.split("-") if w]
    # "Spark-X2.6-4B" is a family and the size it comes in, and the size reads
    # as the family's own rather than as another word: "Spark X2.6:4B". The
    # first size wins, so a quant in the same name is left alone.
    for i in range(1, len(words)):
        if SIZE.match(words[i]):
            words[i - 1] = f"{words[i - 1]}:{words[i]}"
            del words[i]
            break

    label = " ".join(words)
    return f"{label} ({tag})" if tag and tag != "latest" else label


async def healthy():
    """Whether the model endpoint answers at all."""
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(
                f"{settings.model_base_url}/models",
                headers={"Authorization": f"Bearer {settings.model_api_key}"},
            )
            return r.status_code < 500
    except Exception:
        return False


# --- the thinking ladder ----------------------------------------------------

# Every value the endpoint's `think` field accepts as a level, in the order a
# person would read them. A model's own template names the ones it takes and
# rejects the rest, so this list is the question, not the answer.
THINK_LEVELS = ("low", "medium", "high", "max")

# A value no server defines. One that validates `think` refuses it, and one
# that has never heard of the field takes it like any other extra key, which is
# how "this control would do nothing here" is told from "it would do something".
SENTINEL = "__probe__"

# What to offer when the model could not be asked at all: the plain on/off
# pair, which is what a request carried before any of this existed. A model
# whose ladder is `[]` gets no control instead.
FALLBACK_LADDER = [False, True]

# The answer for a model, by id, once it is known. A probe costs a round trip
# and the answer cannot change while the process is up: a different template is
# a different model id.
_LADDERS: dict = {}

# A refused value is refused before the model is touched, so the sentinel's
# answer comes back almost at once and is worth waiting only briefly for.
SENTINEL_TIMEOUT = 5

# A value that is accepted has to be generated to prove it, which on a model
# that is not loaded means loading it first. That is slow once and free later,
# so it is given room rather than being mistaken for a refusal — but not so much
# that the panel sits on "Checking..." behind a model too large to load in time.
# Running out of room is the FALLBACK_LADDER case, which is honest and works.
PROBE_TIMEOUT = 25


async def _send_think(client, model, value):
    """One probe: does this model accept `think=value`?

    `True`, `False`, or `None` when the server could not be asked at all. A
    refused *value* arrives as a stated error, which is an answer; an
    unreachable server is not, and neither is a 404 — that is the server saying
    it does not serve this model, which is a fact about the id rather than about
    the value, and reading it as a refusal would cache a dead control for a
    model that may be pulled a minute later.

    `num_predict` is 1 because only the accept or reject matters here: the probe
    must not spend the model's tokens reasoning about the word "hi".
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "think": value,
        "options": {"num_predict": 1},
    }
    try:
        r = await client.post(chat_url(), json=payload)
    except httpx.HTTPError:
        return None
    if r.status_code == 404:
        return None
    return r.status_code < 400


async def think_ladder(model=None):
    """The `think` values this model takes, cheapest rung first.

    `[]` when nothing here can think: the server took the sentinel, which means
    it never reads this field.

    `None` when nothing could be asked — a server that is down, an id it does
    not serve, or a probe that ran out of time. That is not an answer about the
    model, so it is not cached, and the caller falls back to FALLBACK_LADDER
    rather than losing the toggle. A probe that times out is deliberately not
    read as a refusal: a cold model takes longer to load than it does to say no
    to a value, and half a ladder would hide the rungs it does have.

    The candidates are asked at once, so a cold model pays one round trip
    rather than six.
    """
    name = model or settings.model_name
    if name in _LADDERS:
        return _LADDERS[name]
    # The sentinel first, because its answer decides whether the rest is even a
    # question: a server that never reads `think` would spend a generation on
    # each rung for a ladder nothing will use.
    try:
        async with httpx.AsyncClient(timeout=SENTINEL_TIMEOUT) as c:
            takes_sentinel = await _send_think(c, name, SENTINEL)
    except Exception:
        return None
    if takes_sentinel is None:
        return None
    if takes_sentinel:
        _LADDERS[name] = []
        return []
    asked = (False, True) + THINK_LEVELS
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
            answers = await asyncio.gather(*[_send_think(c, name, v) for v in asked])
    except Exception:
        return None
    if any(a is None for a in answers):
        # Something went unasked. The rungs that answered describe a ladder this
        # run cannot see the rest of, so no ladder is claimed.
        return None
    ladder = [v for v, ok in zip(asked, answers) if ok]
    # `true` is the model's own default, not a step between off and a level: a
    # model that takes it and names no levels gets the plain pair, which is the
    # whole ladder for a thinker with no dial. When it does name levels, plain
    # `true` is one of them already and adds nothing.
    if True in ladder and any(isinstance(v, str) for v in ladder):
        ladder = [v for v in ladder if v is not True]
    _LADDERS[name] = ladder
    return ladder


def cached_ladder(model=None):
    """The ladder for a model if it has already been asked for, else `None`.

    A chat must not wait on a probe: asking costs a round trip, and on a model
    that is not loaded it costs the load as well. The answer is only needed to
    turn an index into a value, and `think_value` already has an honest answer
    for the unknown case, so the call site that would have to wait uses this and
    leaves the asking to the panel that shows the ladder.
    """
    return _LADDERS.get(model or settings.model_name)


def rung_index(stored, ladder):
    """Where a stored `effort` index lands on this model's ladder.

    A choice remembered against one model is read against another's, so the
    index is clamped rather than trusted: a step this model does not have
    lands on the nearest rung it does.

    Nothing stored means the user has not chosen, and the model's own default
    is the honest answer — the rung that is plain `true` when the ladder has
    one, and the top rung otherwise.
    """
    rungs = FALLBACK_LADDER if ladder is None else ladder
    if not rungs:
        return 0
    if stored is None:
        return rungs.index(True) if True in rungs else len(rungs) - 1
    return max(0, min(int(stored), len(rungs) - 1))


def think_value(effort, ladder):
    """The `think` value the stored `effort` index means for this model.

    A model whose ladder is empty is asked for no thinking rather than for a
    value it would refuse.
    """
    rungs = FALLBACK_LADDER if ladder is None else ladder
    return rungs[rung_index(effort, ladder)] if rungs else False


def effort_label(value):
    """A rung as the control names it: `Off`, `On`, or the level capitalized."""
    if value is False:
        return "Off"
    if value is True:
        return "On"
    return str(value).capitalize()


