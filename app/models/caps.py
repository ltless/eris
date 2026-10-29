"""What a model can do, as the endpoint declares it.

Three things here are per model and none of them can be read off a name: the
model list reports one set of ids and the same endpoint can serve a model that
calls tools beside one that ignores the tool list entirely. The endpoint states
this, so it is asked rather than guessed, and the answer cannot change while
the process is up — a different capability set is a different model id.

Asked of `/api/tags`, which answers for the whole list in one request. A server
that does not report capabilities there (llama.cpp's own `/v1/models` does not)
answers `None` for everything, and every caller reads `None` as "unknown, offer
it": a tool list the model ignores costs tokens, but a tool list withheld from a
model that could use it costs the answer.
"""

import httpx

from ..config import settings
from .endpoint import base_url

TAGS_TIMEOUT = 5

# The values `/api/tags` uses for the flags this app acts on.
_TOOL_CAP = "tools"
_VISION_CAP = "vision"
_THINK_CAP = "thinking"

# What a model is known to do, by id, once the endpoint has said so. A model
# the listing does not name is recorded as `None` — nothing was said about it —
# so a bogus id costs one listing read rather than one per request. A model
# pulled later is still unknown, which reads as "offer it" rather than as
# "it cannot", and that is the safe side of a stale cache.
_CAPS: dict = {}


def _flags(entry):
    """The capability names one listing entry declares."""
    caps = entry.get("capabilities")
    return [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else None


async def _read_listing():
    """The listing's flags by model id, or `None` when the server will not say."""
    try:
        async with httpx.AsyncClient(timeout=TAGS_TIMEOUT) as c:
            r = await c.get(base_url() + "/api/tags")
    except httpx.HTTPError:
        return None
    if r.status_code >= 400:
        return None
    try:
        rows = r.json().get("models")
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        # The listing names the model twice, and the id is the one this app
        # stores; `model` is the same string on every server seen so far, but
        # the id is the field the rest of the endpoint answers to.
        mid = row.get("model") or row.get("name")
        flags = _flags(row)
        if isinstance(mid, str) and flags is not None:
            out[mid] = flags
    return out or None


def _capabilities(flags):
    """One model's declared flags as the booleans the app acts on.

    A flag that is absent is `False`, because the listing is the whole of what
    the model says it does. That is different from `None`, which is nothing
    being said at all.
    """
    if flags is None:
        return None
    return {
        "tools": _TOOL_CAP in flags,
        "vision": _VISION_CAP in flags,
        "thinking": _THINK_CAP in flags,
    }


async def capabilities(model=None):
    """What this model declares, or `None` when the endpoint does not say.

    Cached per id: the answer is a property of the model file, and the endpoint
    re-reads it on every listing request otherwise.
    """
    name = model or settings.model_name
    if name in _CAPS:
        return _CAPS[name]
    await listed_capabilities()
    return _CAPS.get(name)


async def listed_capabilities():
    """Every model's flags, by id, from one listing read.

    The listing answers for all of them at once, which is why a route that
    shows the answer asks this rather than asking per model: the reading is
    shared, and it is also what puts the chat path's `cached_capabilities` in
    a position to answer.
    """
    listing = await _read_listing()
    if listing is None:
        return None
    for mid, flags in listing.items():
        _CAPS[mid] = _capabilities(flags)
    return dict(_CAPS)


def cached_capabilities(model=None):
    """What was already asked for, or `None` if it has not been.

    A chat must not wait on the listing any more than it waits on a probe, and
    the unknown case has an honest answer — offer everything — so the call site
    that would have to wait uses this and leaves the asking to the routes that
    show the answer.
    """
    name = model or settings.model_name
    return _CAPS.get(name)