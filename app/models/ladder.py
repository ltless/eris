"""Which `think` values a model takes, and what a stored index means for it.

The endpoint's `think` field is not one control with a fixed scale: a model's
own template names the levels it accepts and rejects the rest, so the set has to
be asked for per model. Asking costs a round trip and the answer cannot change
while the process is up, so it is asked once and kept.

Asked two ways, cheapest first. `/api/show` reports the values the server
accepts, which is the ladder itself; a server that reports nothing there is
probed with a request per rung, which can only see accept-or-reject and so
cannot tell "this model ignores the field" from "this model takes everything".
"""

import asyncio

import httpx

from ..config import settings
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

    A server that names its values has already answered the question the probe
    would ask, and answered it better: the probe sees only accept-or-reject, so
    a model that takes every value looks the same as one that reads none of
    them. `values` is `[false, true]` for a thinker with a plain switch and the
    level names for one with a dial, in the server's own order.
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
        async with httpx.AsyncClient(timeout=SHOW_TIMEOUT) as c:
            r = await c.post(base_url() + "/api/show", json={"model": name})
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

    A server that states its values on `/api/show` is believed and asked
    nothing more: that is the ladder, already ordered, and it is the only
    answer that can tell a single switch from a dial.

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
    shown = await _shown_ladder(name)
    if shown is not None:
        _LADDERS[name] = shown
        return shown
    # Nothing stated, so the values are asked for directly. The sentinel comes
    # first, because its answer decides whether the rest is even a question: a
    # server that never reads `think` would spend a generation on each rung for
    # a ladder nothing will use.
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


def think_for_request(thinking, model, tools):
    """The `think` value a request should actually carry.

    On some templates `false` does not mean "think less": it takes the tool
    list with it, and the model answers from memory — inventing presidents,
    inventing prices — while the search it would have run never happens. So a
    model that names levels is kept off `false` on a tool round: it sends the
    lowest level it names instead, which is the nearest thing to off that does
    not cost the tools.

    A model whose ladder is only the plain switch has no such rung, and there
    `false` is the honest value: it was tried against both shapes here and the
    tools still arrive. The field is never dropped to stand in for it — a
    missing `think` is the server's own default, which on a thinker is on, and
    that is the opposite of what this control was just asked for.
    """
    if thinking is not False or not tools:
        return thinking
    levels = [v for v in (cached_ladder(model) or []) if isinstance(v, str)]
    return levels[0] if levels else False
