"""Assembling what the model gets to see.

One window, one function: what the model is sent is decided here, and the
engine does not truncate anything itself.
"""

from ..encoding import image_part_url, text_part_text

# Room for the system prompt, and room for the turns that follow it.
PROMPT_LIMIT = 8_000
SESSION_LIMIT = 8_000

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


def recent(messages):
    """The newest whole turns that fit, oldest first.

    Turns are kept or dropped as a unit, and the window opens on a user turn:
    an answer whose question fell off reads as the model talking to itself. The
    cap is both a character room and a message count, so a run that made many
    small tool calls is bounded the same way as one that made few large ones.
    """
    kept = []
    spent = 0
    for m in reversed(messages):
        cost = _message_cost(m)
        if kept and (spent + cost > SESSION_LIMIT or len(kept) >= MAX_MESSAGES):
            break
        kept.append(m)
        spent += cost
    kept.reverse()
    while len(kept) > 1 and kept[0].get("role") != "user":
        kept.pop(0)
    return kept or list(messages[-1:])


def _message_cost(message):
    """Rough size of one turn. An image is a fixed estimate: the payload is a
    data URL, and counting its characters would call one picture the size of a
    whole conversation."""
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        text = sum(len(t) for t in map(text_part_text, content) if t)
        images = sum(1 for p in content if image_part_url(p))
        return text + 4_000 * images
    return 0
