"""Which `think` values a model takes, and what a stored index means for it.

The set is per model and asked once, because asking costs a round trip and the
answer cannot change while the process is up. `/api/show` is believed when it
states the values; otherwise each rung is probed, which cannot tell "ignores the
field" from "takes everything" — that is what the sentinel is for.
"""

import asyncio

import httpx

from ..config import settings
from .client import client as http
from .endpoint import base_url, chat_url

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

# A probe already running, by model id. `/api/models` starts one in the
# background and the effort control asks for the same answer moments later, so
# the second caller waits on the first instead of probing beside it.
_PENDING: dict = {}

# A refused value is refused before the model is touched, so the sentinel's
# answer comes back almost at once and is worth waiting only briefly for.
SENTINEL_TIMEOUT = 5

# `/api/show` reads metadata only: no load, no generation, so it answers fast
# even for a model that is cold.
SHOW_TIMEOUT = 10

# A value that is accepted has to be generated to prove it, which on a model
# that is not loaded means loading it first. That is slow once and free later,
# so it is given room rather than being mistaken for a refusal — but not so much
# that the panel sits on "Checking..." behind a model too large to load in time.
# Running out of room is the FALLBACK_LADDER case, which is honest and works.
PROBE_TIMEOUT = 25


def _stated(values):
    """The ladder a server stated, or `None` when it stated nothing usable.

    A stated answer beats the probe: the probe sees only accept-or-reject, so a
    model that takes every value looks like one that reads none of them.
    `values` is `[false, true]` for a plain switch, level names for a dial.
    """
    if not isinstance(values, list) or not values:
        return None
    rungs = [v for v in values if isinstance(v, str)]
    if rungs:
        # Named levels are the ladder; a plain `true` would be a duplicate rung.
        if False in values:
            rungs.insert(0, False)
        return rungs
    switch = [v for v in values if isinstance(v, bool)]
    return switch or None


async def _shown_ladder(name):
    """What the server says this model accepts, or `None` if it says nothing."""
    try:
        r = await http().post(base_url() + "/api/show", json={"model": name},
                              timeout=SHOW_TIMEOUT)
    except httpx.HTTPError:
        return None
    if r.status_code >= 400:
        return None
    try:
        shown = r.json().get("thinking")
    except ValueError:
        return None
    if not isinstance(shown, dict):
        return None
    return _stated(shown.get("values"))


async def _send_think(c, model, value, timeout):
    """One probe: does this model accept `think=value`?

    `True`, `False`, or `None` when the server could not be asked. A refused
    value is a stated error and counts as an answer; a 404 is a fact about the
    id, so reading it as a refusal would cache a dead control for a model that
    may be pulled a minute later.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "think": value,
        "options": {"num_predict": 1},
    }
    try:
        r = await c.post(chat_url(), json=payload, timeout=timeout)
    except httpx.HTTPError:
        return None
    if r.status_code == 404:
        return None
    return r.status_code < 400


async def think_ladder(model=None):
    """The `think` values this model takes, cheapest rung first.

    `[]` when the server took the sentinel, so it never reads the field. `None`
    when nothing could be asked; that is not an answer about the model, so it is
    not cached and the caller falls back to FALLBACK_LADDER. A probe that times
    out counts as unasked, not refused: a cold model loads slower than it says
    no, and half a ladder would hide the rungs it has.
    """
    name = model or settings.model_name
    if name in _LADDERS:
        return _LADDERS[name]
    # A probe belongs to the loop that started it, so a caller on another loop
    # starts its own rather than awaiting one that loop can no longer run.
    pending = _PENDING.get(name)
    if pending is not None and pending.get_loop() is asyncio.get_running_loop():
        return await asyncio.shield(pending)
    task = asyncio.ensure_future(_ask_ladder(name))
    _PENDING[name] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _PENDING.get(name) is task:
            _PENDING.pop(name, None)


async def _ask_ladder(name):
    """The probe itself, kept out of `think_ladder` so one is shared."""
    shown = await _shown_ladder(name)
    if shown is not None:
        _LADDERS[name] = shown
        return shown
    # Nothing stated, so the values are asked for directly. The sentinel comes
    # first, because its answer decides whether the rest is even a question: a
    # server that never reads `think` would spend a generation on each rung for
    # a ladder nothing will use.
    try:
        takes_sentinel = await _send_think(http(), name, SENTINEL, SENTINEL_TIMEOUT)
    except Exception:
        return None
    if takes_sentinel is None:
        return None
    if takes_sentinel:
        _LADDERS[name] = []
        return []
    asked = (False, True) + THINK_LEVELS
    try:
        answers = await asyncio.gather(
            *[_send_think(http(), name, v, PROBE_TIMEOUT) for v in asked]
        )
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

    A chat must not wait on a probe: on a model that is not loaded it costs the
    load too. This call site leaves the asking to the panel that shows it.
    """
    return _LADDERS.get(model or settings.model_name)


def rung_index(stored, ladder):
    """Where a stored `effort` index lands on this model's ladder.

    The index is clamped, not trusted: a step this model lacks lands on the
    nearest rung it has. Nothing stored means the model's own default.
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


def think_for_request(thinking, model, tools):
    """The `think` value a request should actually carry.

    On some templates `false` takes the tool list with it, so the model answers
    from memory and the search never runs. A model that names levels therefore
    sends its lowest rung on a tool round. A plain-switch model has no such rung
    and keeps `false`; the field is never dropped, since a missing `think` is
    the server default, which on a thinker is on.
    """
    if thinking is not False or not tools:
        return thinking
    levels = [v for v in (cached_ladder(model) or []) if isinstance(v, str)]
    return levels[0] if levels else False
