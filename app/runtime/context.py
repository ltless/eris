"""Assembling what the model gets to see.

One window, one function: what the model is sent is decided here, and the
engine does not truncate anything itself.
"""

import json

from ..encoding import image_part_url, text_part_text

# Text limits used before the runner reports its context.
PROMPT_LIMIT = 32_000
SESSION_LIMIT = 16_000

# The most messages one request may carry. A long tool round can add a pair of
# messages per call, and a server that pairs an assistant call with its answer
# rejects the request when the pair is split, so the cut opens on a user turn.
MAX_MESSAGES = 60

TRUNCATED = "\n[... truncated]"


def _slice(text, limit):
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit].rstrip() + TRUNCATED


def assemble(*, base_prompt, today, instruction=""):
    """The system prompt, within budget.

    The base prompt and the instruction go first, since a truncation that drops
    them is a broken request.
    """
    parts = [_slice(base_prompt, PROMPT_LIMIT)]
    if today:
        parts.append(f"Today is {today}.")
    if instruction:
        parts.append(instruction)
    return "\n\n".join(p for p in parts if p and p.strip())


def history_budget(window, system, tools=None):
    if not window:
        return SESSION_LIMIT
    # Reserve space for an answer. Three UTF-8 bytes per token is a prompt
    # sizing heuristic; the context meter uses the provider's actual counts.
    output_room = min(2048, window // 4)
    schema_bytes = len(json.dumps(tools, ensure_ascii=False).encode("utf-8")) if tools else 0
    return max(0, (window - output_room) * 3 - _message_cost(system) - schema_bytes)


def recent(messages, limit=SESSION_LIMIT, *, max_messages=MAX_MESSAGES):
    """The newest whole turns that fit, oldest first.

    Turns are kept or dropped as a unit, and the window opens on a user turn:
    an answer whose question fell off reads as the model talking to itself. The
    cap is both a character room and a message count, so a run that made many
    small tool calls is bounded the same way as one that made few large ones.
    """
    starts = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    if not starts:
        return list(messages[-1:])
    start, end = starts[-1], len(messages)
    last = list(messages[start:])
    spent = sum(map(_message_cost, last))
    if spent > limit:
        return _trim_tool_results(last, limit)
    for previous in reversed(starts[:-1]):
        cost = sum(map(_message_cost, messages[previous:start]))
        if (max_messages is not None and end - previous > max_messages) or spent + cost > limit:
            break
        spent += cost
        start = previous
    return list(messages[start:])


def _trim_tool_results(messages, limit):
    tools = [i for i, m in enumerate(messages) if m.get("role") == "tool"
             and isinstance(m.get("content"), str)]
    if not tools:
        return messages
    fixed = sum(_message_cost({**m, "content": ""}) if i in tools else _message_cost(m)
                for i, m in enumerate(messages))
    remaining = max(0, limit - fixed)
    for i in reversed(tools):
        content = messages[i]["content"]
        served = content.encode("utf-8")
        if len(served) > remaining:
            marker = TRUNCATED if remaining >= len(TRUNCATED) else ""
            content = served[:remaining - len(marker)].decode("utf-8", errors="ignore") + marker
            messages[i] = {**messages[i], "content": content}
        remaining -= len(content.encode("utf-8"))
    return messages


def _message_cost(message):
    """Rough size of one turn. An image is a fixed estimate: the payload is a
    data URL, and counting its characters would call one picture the size of a
    whole conversation."""
    content = message.get("content")
    overhead = 24
    if message.get("tool_calls"):
        overhead += len(json.dumps(message["tool_calls"], ensure_ascii=False).encode("utf-8"))
    if isinstance(content, str):
        return overhead + len(content.encode("utf-8"))
    if isinstance(content, list):
        text = sum(len(t.encode("utf-8")) for t in map(text_part_text, content) if t)
        images = sum(1 for p in content if image_part_url(p))
        return overhead + text + 4_000 * images
    return overhead
