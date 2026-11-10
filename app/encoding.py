"""The small helpers the routes used to inline.

Media naming, the token/id generators, and the shape of a content part were
inside `api.py`'s handlers and its modules. They are pure functions over bytes,
a filename, and a payload, which makes them testable on their own.

The content parts are the one wire shape this module owns: the browser builds
them, so every reader here answers for a part of the wrong shape rather than
raising on whichever caller was handed it.
"""

import secrets
from pathlib import Path

# Anchor asset paths to the repo root so uvicorn works from any CWD.
ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
MEDIA_DIR = STATIC_DIR / "uploads"


def new_id():
    return secrets.token_hex(16)


def new_token():
    """The suffix that keeps one upload's filename from colliding with the
    next. Eight hex characters is the only length any caller asked for."""
    return secrets.token_hex(4)


def image_extension(content_type):
    ct = content_type or ""
    for token, ext in (("png", "png"), ("jpeg", "jpg"), ("jpg", "jpg"), ("webp", "webp"),
                       ("gif", "gif"), ("avif", "avif")):
        if token in ct:
            return ext
    return "jpg"


def image_part_url(part):
    """The URL inside one OpenAI-shaped image content part, or `None`.

    The browser builds these parts and three readers on this side open them —
    the transport strips the prefix, the cost estimate counts one per picture,
    and the stored turn decodes the bytes — so the nesting is stated once here.
    """
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return None
    holder = part.get("image_url")
    url = holder.get("url") if isinstance(holder, dict) else None
    return url if isinstance(url, str) else None


def text_part_text(part):
    """The text inside one OpenAI-shaped text content part, or `None`.

    The sibling of `image_part_url`, and total for the same reason: these parts
    come from the browser, so a part of the wrong shape answers "not a text
    part" rather than raising on whichever reader was handed it. An empty text
    part is `""`, not `None` — the two are different answers.
    """
    if not isinstance(part, dict) or part.get("type") != "text":
        return None
    text = part.get("text")
    return text if isinstance(text, str) else ""
