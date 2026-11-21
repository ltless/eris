"""Provider transport: speaks to the model server.

This is the request itself: NDJSON framing, tool call shapes, and the error the
runtime catches. The endpoint is llama.cpp's native `/api/chat`; the
OpenAI-shaped `/v1/...` set is used only for the model list.
"""

import json
import re

import httpx

from ..encoding import image_part_url, text_part_text
from .endpoint import auth_headers, base_url, chat_url, model_for, models_url
from .ladder import cached_reasoning, request_effort
from .metadata import (
    cached_model_info, cached_properties, configured_context, llama_window, positive_int,
    requested_context, runtime_status, server_properties,
)


def to_native(messages):
    """OpenAI-shaped content parts to the native `messages` + `images` shape."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            text = "".join(t for t in map(text_part_text, c) if t)
            # A part can be malformed; it is dropped rather than allowed to
            # raise here, because this runs before the request's try and the
            # error would escape the generator instead of reaching the caller.
            imgs = [u.split(",", 1)[-1] for u in map(image_part_url, c) if u]
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
    if not isinstance(call, dict):
        return {}
    fn = call.get("function")
    if not isinstance(fn, dict):
        fn = {}
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
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details") or {}
        return {
            field: count for field, count in {
                "prompt_eval_count": usage.get("prompt_tokens"),
                "eval_count": usage.get("completion_tokens"),
                "prompt_eval_cached_count": details.get("cached_tokens") if isinstance(details, dict) else None,
            }.items() if type(count) is int and count >= 0
        } or None
    if not chunk.get("done"):
        return None
    out = {}
    for name in ("prompt_eval_count", "prompt_eval_cached_count", "eval_count",
                 "eval_duration", "load_duration", "context_window", "context_length"):
        value = chunk.get(name)
        if type(value) in (int, float) and value >= 0:
            out[name] = value
    return out or None


def parse_chunk(line, *, tool_fragments=None):
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
    # A line that parses as anything but an object is not a frame: `null`, a
    # bare number, an array. Read as one anyway it raises `AttributeError`
    # inside the stream, where nothing catches it, and the turn ends with no
    # closing frame and no error.
    if not isinstance(chunk, dict):
        return []
    msg = chunk.get("message")
    if not isinstance(msg, dict):
        choices = chunk.get("choices") or []
        first = choices[0] if isinstance(choices, list) and choices else None
        delta = (first or {}).get("delta") if isinstance(first, dict) else None
        msg = delta if isinstance(delta, dict) else {}
    events = []
    if msg.get("content"):
        events.append(("delta", msg["content"]))
    think = msg.get("thinking") or msg.get("reasoning_content")
    if think:
        events.append(("thinking", think))
    calls = msg.get("tool_calls")
    if isinstance(calls, list) and calls:
        # A malformed entry is dropped here rather than normalized into a call
        # with no name, which would be offered to the registry as an unknown
        # tool and reported as one.
        if tool_fragments is None:
            events.append(("tool_calls", [normalize_call(c) for c in calls if isinstance(c, dict)]))
        else:
            _accumulate_calls(tool_fragments, calls)
    # The closing frame carries both a last message and the counts, so this is
    # an addition to that frame rather than a branch away from it.
    done = parse_metrics(chunk)
    if done:
        events.append(("metrics", done))
    return events


def _accumulate_calls(parts, calls):
    for call in calls:
        if not isinstance(call, dict) or type(call.get("index")) is not int:
            continue
        item = parts.setdefault(call["index"], {
            "type": "function", "id": "", "function": {"name": "", "arguments": ""},
        })
        if call.get("id"):
            item["id"] = call["id"]
        fn = call.get("function") or {}
        if isinstance(fn, dict):
            name, args = fn.get("name"), fn.get("arguments")
            if isinstance(name, str):
                item["function"]["name"] += name
            if isinstance(args, str):
                item["function"]["arguments"] += args


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


def _compatible_messages(messages):
    out = []
    for message in messages:
        if not message.get("tool_calls"):
            out.append(message)
            continue
        calls = []
        for call in message["tool_calls"]:
            fn = call["function"]
            args = fn.get("arguments")
            calls.append({**call, "type": "function",
                          "function": {**fn, "arguments": args if isinstance(args, str)
                                       else json.dumps(args or {})}})
        out.append({**message, "tool_calls": calls})
    return out


def _chat_payload(messages, model, thinking, tools, ctx, limit, *, compatible=False):
    value = request_effort(cached_reasoning(model), thinking)
    payload = {
        "model": model, "stream": True,
        "messages": _compatible_messages(messages) if compatible else to_native(messages),
    }
    if compatible:
        payload["stream_options"] = {"include_usage": True}
        if isinstance(value, str):
            payload["reasoning_effort"] = value
        if limit:
            payload["max_tokens"] = limit
    else:
        if value is not None:
            payload["think"] = value
        options = {}
        if ctx:
            options["num_ctx"] = ctx
        if limit:
            options["num_predict"] = limit
        if options:
            payload["options"] = options
    if tools:
        payload["tools"] = tools
    return payload


async def _call_metrics(metrics, model, ctx, props):
    window = positive_int(metrics.get("context_window")) or positive_int(metrics.get("context_length"))
    source = "reported" if window else None
    requested = requested_context(ctx)
    if not window and props:
        window, source = llama_window(props), "runner"
    if not window and ("prompt_eval_count" in metrics or "eval_count" in metrics):
        loaded = await runtime_status(model, timeout=2)
        observed = positive_int((loaded or {}).get("context_length"))
        # A runner may have been reassigned between generation and the poll.
        if not requested or observed == requested:
            window = observed
        source = "runner" if window else None
    if not window:
        window = requested or configured_context(cached_model_info(model))
        source = "requested" if window else None
    return {**metrics, "model": model, "context_window": window, "context_source": source}


async def stream_completion(messages, model=None, thinking=False, tools=None, ctx=None,
                            limit=None):
    model = model_for(model)
    props = cached_properties(model)
    url = chat_url()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as c:
            for _ in range(2):
                url = base_url() + "/v1/chat/completions" if props else chat_url()
                payload = _chat_payload(messages, model, thinking, tools, ctx, limit,
                                        compatible=bool(props))
                async with c.stream("POST", url, json=payload, headers=auth_headers()) as r:
                    if r.status_code >= 400:
                        body = (await r.aread()).decode(errors="replace")
                        if r.status_code in {404, 405} and not props and cached_model_info(model) is None:
                            props = await server_properties(model)
                            if props:
                                continue
                        raise ModelError(f"model server returned {r.status_code}: {body[:400]}")
                    fragments = {} if props else None
                    async for line in r.aiter_lines():
                        for kind, value in parse_chunk(line, tool_fragments=fragments):
                            if kind == "metrics":
                                value = await _call_metrics(value, model, ctx, props)
                                value["effort"] = payload.get("reasoning_effort") if props else payload.get("think")
                            yield kind, value
                    if fragments:
                        yield "tool_calls", [normalize_call(fragments[i]) for i in sorted(fragments)]
                    return
    except httpx.HTTPError as e:
        raise ModelError(f"cannot reach the model server at {url}: {e}") from e


TITLE_PROMPT = (
    "Write the title of this conversation as three to six words. Name the "
    "topic as a noun phrase — not the question, not a sentence, no quotes. "
    "Answer in the language of the message below. Reply with the title alone."
)

# One line, no wrapping: a model that answers with a paragraph leaves the
# first line as the title, and `num_predict` below stops it writing one.
TITLE_LIMIT = 24


async def title_for(prompt, model=None, ctx=None):
    """A short title naming what the first message is about.

    A title is a second model call, so it is allowed to fail quietly: an
    unreachable server, or an answer with no words in it, leaves the caller
    with the message's own opening words instead — what it had before.
    """
    text = ""
    try:
        async for kind, value in stream_completion(
            [
                {"role": "system", "content": TITLE_PROMPT},
                {"role": "user", "content": prompt[:2000]},
            ],
            model=model,
            thinking=False,
            ctx=ctx,
            limit=TITLE_LIMIT,
        ):
            if kind == "delta":
                text += value
    except ModelError:
        return ""
    for raw in text.splitlines():
        line = clean_title(raw)
        if line:
            # No length cut here: the one place that stores a title already
            # bounds it (`chats.short_title`), and a cut here would be a second
            # limit that has to be kept in step with that one.
            return line
    return ""


def clean_title(line):
    """One line of model output as a bare title, or empty if nothing is left.

    Models wrap the title in whatever the surrounding text used — a markdown
    heading, a bullet, a number, a `Title:` label — and the label often lands
    alone on the line before it. Each of those is peeled off, and a line that
    is only decoration comes back empty so the caller moves on to the next.
    """
    line = line.strip()
    for _ in range(3):
        before = line
        line = line.strip("\"'“”‘’*_`# ")
        for prefix in ("title:", "judul:", "judul", "title"):
            if line.lower().startswith(prefix):
                line = line[len(prefix) :].lstrip(" :.-\t")
                break
        # `1.`, `2)` and `-` are list markers, not part of a name.
        line = re.sub(r"^(?:\d+[.)]|[-–—•])\s*", "", line)
        if line == before:
            break
    # A line left ending in a colon is the lead-in to the name rather than the
    # name — `Here is the title:` — and reads as an answer to nothing.
    if line.endswith(":"):
        return ""
    return line.strip("\"'“”‘’*_` ") if line else ""

async def list_models():
    """Model ids the endpoint reports. Empty when it cannot be reached."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(models_url(), headers=auth_headers())
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        return []


# A parameter count that stands on its own: "4B", "0.5B", "70B".
SIZE = re.compile(r"^(\d+(?:\.\d+)?)B$", re.I)


def model_label(mid):
    """A model id as a person would read it.

    Display only; the stored id stays the handle the endpoint accepts. The
    registry path, the GGUF suffix and a `latest` tag name the file, so they go.
    Any other tag stays: in Ollama it is the variant, and dropping it would give
    two entries one name.
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
            r = await c.get(models_url(), headers=auth_headers())
            return r.status_code < 500
    except Exception:
        return False

