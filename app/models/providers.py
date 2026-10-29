"""Provider transport: speaks to the model server.

The endpoint's URL shape lives in `endpoint`, and the `think` ladder in
`ladder`; this module is the request itself — the NDJSON framing, the tool
call shapes, and the error the runtime catches.

The endpoint is the llama.cpp native `/api/chat` route; the OpenAI-shaped
`/v1/...` set is used only for the model list.
"""

import json
import re

import httpx

from ..config import settings
from .endpoint import chat_url
from .ladder import think_for_request


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


async def stream_completion(messages, model=None, thinking=False, tools=None, ctx=None):
    payload = {
        "model": model or settings.model_name,
        "messages": to_native(messages),
        "stream": True,
    }
    # `false` skips the thinking channel, `true` takes the model's own
    # default, and a level names a stated effort. The ladder is per model,
    # so the value arrives already resolved against think_ladder rather
    # than assumed here; `think_for_request` keeps a tool round off `false`
    # on the models where that rung also switches the tools off.
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

