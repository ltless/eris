"""The small helpers the routes used to inline.

Media naming and the token/id generators were inside `api.py`'s handlers. They
are pure functions over bytes and a filename, which makes them testable on
their own.
"""

import secrets
from pathlib import Path

# Anchor asset paths to the repo root so uvicorn works from any CWD.
ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
MEDIA_DIR = STATIC_DIR / "uploads"


def new_id():
    return secrets.token_hex(16)


def new_token(n=4):
    return secrets.token_hex(n)


def image_extension(content_type):
    ct = content_type or ""
    for token, ext in (("png", "png"), ("jpeg", "jpg"), ("jpg", "jpg"), ("webp", "webp"),
                       ("gif", "gif"), ("avif", "avif")):
        if token in ct:
            return ext
    return "jpg"
