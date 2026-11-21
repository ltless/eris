"""The one execution loop.

The loop calls the model, runs the tools it requested, feeds results back, and
stops when the model answers, the budget runs out, or a limit is reached.

It does not decide permissions (the tool registry does), does not know tools
by name (registration does), and does not write to the database directly.
Everything it does is reported as an event, in the order it happened.
"""

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator

from .. import errors
from ..models import ModelError, cached_capabilities, merge_tool_calls, stream_completion
from ..security import ALLOWED
from ..tools.plan import step_line
from ..tools.search import NAME as WEB_SEARCH
from ..tools.registry import TAGS, ToolContext, ToolResult, as_args, registry
from . import events
from .budgets import Budget, BudgetExceeded, clip
from .context import history_budget, recent
from .events import emit
from .routing import Route, is_news_lookup, links_from_search, missing_news_query, provides_data


@dataclass
class RunRequest:
    """One question and the conditions it runs under."""

    messages: list                       # [{role, content}] with system prompt first
    model: str | None = None
    thinking: bool | str | None = None
    ctx_tokens: int | None = None
    tags: tuple = TAGS                   # which tools to offer
    budget: Budget | None = None
    tool_context: ToolContext | None = None
    routing: Route | None = None
    window_tokens: int | None = None


@dataclass
class RunResult:
    """What came back. `answer` is the model's prose; the rest is the record."""

    answer: str = ""
    thinking: list = field(default_factory=list)
    # The prose of each round, keyed by round number. `answer` is the last
    # round's text alone; a run that answered, searched, then answered again
    # has two entries here and only a reload needs them.
    answers: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    stop_reason: str = "answered"
    budget: dict = field(default_factory=dict)


def tool_message(call, content):
    m = {"role": "tool", "content": content}
    if call.get("id"):
        m["tool_call_id"] = call["id"]
    return m


def call_id(call):
    """The model's own id for one call, or `""` when it named none.

    A round runs its calls at once and reports them as each finishes, so the
    round alone cannot say which call an event belongs to; the id is what the
    browser and the stored turn both match on.
    """
    return str(call.get("id") or "")


def _with_plan(messages, plan):
    """The conversation sent this round, with the run's own plan on the end.

    Appended rather than spliced in, so the prompt before it is unchanged and
    the server's prefix cache still matches. A copy is returned; the stored
    history stays free of the reminder.

    `user`, not `system`: several chat templates, Llama 3's among them, refuse a
    system turn anywhere but the first, and the server rejects the whole request
    rather than ignoring it.
    """
    if not plan:
        return messages
    lines = ["The plan you recorded for this task, so far:"]
    for i, s in enumerate(plan, 1):
        lines.append(step_line(i, s))
    lines.append("Keep going through it, and call plan again when a step changes.")
    return messages + [{"role": "user", "content": "\n".join(lines)}]


_OPEN = "<tool_call>"
_CLOSE = "</tool_call>"

# Sent when a round has nothing else to do and still has no prose to show,
# which happens two ways: the model reasoned to the end and stopped, or a
# tool-less pass turned a tool call into text — the server only parses calls
# when the request carries the tool list. Either way the answer is asked for
# once more, plainly.
_NO_ANSWER = (
    "That reply gave the reader no answer. Answer in prose now, from what the "
    f"run already has, without any tool call and without {_OPEN} tags."
)

# What the reader is told when that ask did not take either. There is no answer
# to show, and the markup some models produce instead is not one.
_NO_ANSWER_SORRY = (
    "The model produced no answer, and said nothing when asked again."
)


def _partial_prefix(text):
    """How many trailing characters could still turn out to open a call."""
    for n in range(min(len(_OPEN) - 1, len(text)), 0, -1):
        if text.endswith(_OPEN[:n]):
            return n
    return 0


class _ToolCallFilter:
    """Drops `<tool_call>` blocks from an answer as it streams.

    The markup arrives in pieces, so a tail as long as the opening tag is held
    back until it is known not to start one. A block that never closes is
    dropped whole: it is half a call, not an answer.
    """

    def __init__(self):
        self._buf = ""
        self._inside = False
        self._fed = 0
        self._out = 0

    @property
    def dropped(self):
        """Characters taken out of the answer as markup."""
        return self._fed - self._out

    def feed(self, piece):
        self._fed += len(piece)
        self._buf += piece
        out = []
        while True:
            if self._inside:
                i = self._buf.find(_CLOSE)
                if i < 0:
                    break
                self._buf = self._buf[i + len(_CLOSE):]
                self._inside = False
                continue
            i = self._buf.find(_OPEN)
            if i >= 0:
                out.append(self._buf[:i])
                self._buf = self._buf[i + len(_OPEN):]
                self._inside = True
                continue
            keep = _partial_prefix(self._buf)
            out.append(self._buf[:-keep] if keep else self._buf)
            self._buf = self._buf[-keep:] if keep else ""
            break
        text = "".join(out)
        self._out += len(text)
        return text

    def flush(self):
        # The end of the stream is not a place a call can still open: nothing
        # more arrives, so a held-back tail is answer text and is emitted. The
        # hold-back in `feed` still does its job while the stream is live.
        if self._inside:
            self._buf = ""
            self._inside = False
            return ""
        rest = self._buf
        self._buf = ""
        self._out += len(rest)
        return rest


def _answer_ask(messages, plan):
    """The conversation plus the note that sends the model back to prose."""
    return _with_plan(messages, plan) + [{"role": "user", "content": _NO_ANSWER}]


def _window(messages, window=None, tools=None):
    """The conversation as the model gets it: the system prompt, then the newest
    turns that fit. A round appends a pair of messages per call, so the window
    is recomputed each round rather than decided once at the door."""
    if len(messages) <= 1:
        return messages
    limit = history_budget(window, messages[0], tools)
    history = recent(messages[1:], limit, max_messages=None) if window else recent(messages[1:], limit)
    return messages[:1] + history


class Engine:
    """Runs one request and yields its events."""

    def __init__(self, request: RunRequest):
        self.req = request
        self.budget = request.budget or Budget()
        self.ctx = request.tool_context or ToolContext()
        self.result = RunResult()
        self._window_tokens = request.window_tokens or request.ctx_tokens
        self.thinking: list[str] = []
        # Track which sources the run has already seen, to know when a
        # repeated search finds nothing new and should stop.
        self._seen_sources: set = set()
        self._new_sources = 0
        # What the current round's call produced: characters of thinking, and
        # characters dropped as a tool call written into the answer channel.
        # Reset at the top of each round; read to tell a round that said
        # nothing from one that thought, or from one that wrote markup.
        self._thought = 0
        self._markup = 0
        declared = cached_capabilities(request.model)
        offer = declared is None or declared.get("tools", True)
        specs = registry.specs_for(self.ctx, request.tags) if offer else []
        self._specs = specs or None
        self._available = {spec["function"]["name"]: spec for spec in specs}
        self._expanded = False
        self._has_data = False
        self._found_links = False
        self._invalid_calls: set[str] = set()
        self._blocked_tools: set[str] = set()
        self._repair_names: tuple[str, ...] = ()
        self._repair_used = False
        self._news_attempted = False
        self._news_dispatched = False
        self._chart_symbol = ""
        # The final pass keeps the tools that declared `last_pass` and drops the
        # rest. A model that still wants to record a step can call it; one that
        # wants to keep searching has no schema to call, so the search ends
        # where the budget says. Without any list at all a model reaches for the
        # tool anyway and writes the call into the answer channel, which the
        # reader then sees as markup.
        kept = {t.name for t in registry.all() if t.last_pass}
        if request.routing is not None:
            kept.intersection_update(request.routing.tools)
        answer_specs = [s for s in specs if s["function"]["name"] in kept]
        # The names the answering pass offers, and the list of schemas it offers,
        # are the same set: the final pass runs a call only if it was offered, so
        # both are built here from one pass over the registry.
        self._answer_names = frozenset(s["function"]["name"] for s in answer_specs)
        self._answer_specs = answer_specs or None

    def _round_specs(self):
        route = self.req.routing
        if route is None:
            return self._specs
        if self._repair_names:
            names = self._repair_names
            self._repair_names = ()
        else:
            names = route.tools
            if self._found_links:
                names = ("fetch_url",) + names
            if self._expanded:
                names = names[:1] + route.fallback + names[1:]
            if self._has_data:
                names = route.after_data + names
            if self._news_pending() and WEB_SEARCH in names:
                names = names[:1] + (WEB_SEARCH,) + names[1:]
        offered = [self._available[name] for name in dict.fromkeys(names)
                   if name in self._available and name not in self._blocked_tools]
        return offered[:3] or None

    def _widen_route(self, round_no):
        route = self.req.routing
        if (route is None or not route.needs_data or self._has_data or self._expanded
                or round_no + 1 >= self.budget.rounds or self.budget.calls_left() <= 1):
            return False
        options = (name for name in route.tools + route.fallback
                   if name in self._available and name not in self._blocked_tools)
        if not any(registry.get(name).permission in ALLOWED for name in options):
            return False
        self._expanded = True
        return True

    def _news_pending(self):
        route = self.req.routing
        return (route is not None and route.news_query is not None
                and WEB_SEARCH in self._available and not self._news_attempted)

    async def _search_requested_news(self, messages, round_no):
        self._news_dispatched = True
        self._news_attempted = True
        query = missing_news_query(self.req.routing, self._chart_symbol)
        call = {"id": f"eris-news-{round_no}", "type": "function",
                "function": {"name": WEB_SEARCH, "arguments": {"query": query}}}
        messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
        async for event in self._run_calls([call], messages, round_no, allowed={WEB_SEARCH}):
            yield event

    async def run(self) -> AsyncIterator[events.Event]:
        """Drive the loop, yielding every event as it happens.

        The generator's return value is the RunResult, reachable afterwards as
        `engine.result`, because a caller streaming events cannot also collect
        a return value across an async generator boundary.
        """
        try:
            async for event in self._loop():
                yield event
        except BudgetExceeded as e:
            self.result.stop_reason = "budget"
            yield emit(
                events.TASK_FAILED, reason="budget",
                code=errors.BUDGET_EXCEEDED, message=str(e),
            )
        except ModelError as e:
            self.result.stop_reason = errors.MODEL_ERROR
            yield emit(
                events.TASK_FAILED, reason=errors.MODEL_ERROR,
                code=errors.MODEL_ERROR, message=str(e),
            )
        self.result.budget = self.budget.used()
        # One entry per round, empty where a round thought nothing. The browser
        # places a card by its round number, so dropping the empty slots would
        # shift every later round's thinking onto an earlier round's cards.
        self.result.thinking = list(self.thinking)

    async def _loop(self):
        messages = list(self.req.messages)

        rounds = self.budget.rounds
        # Extra rounds only if the model-call budget can pay for them.
        extra = min(self.budget.extra_rounds, self._headroom())
        round_no = 0
        while True:
            # Past the soft allowance, grant another round only if the previous
            # one found new sources. A model that can't find new queries gets
            # the same pages back, so an unproductive round ends the search.
            final = round_no >= rounds and (extra <= 0 or self._new_sources == 0)
            if round_no >= rounds and not final:
                extra -= 1
            self._new_sources = 0
            if final and self._news_pending() and self.budget.spent_tools < self.budget.tool_calls:
                self.thinking.append("")
                async for event in self._search_requested_news(messages, round_no):
                    yield event
                round_no += 1

            # The final pass keeps `plan` and drops the busier tools, so the
            # model answers from what it has rather than searching until the
            # budget dies.
            specs = self._answer_specs if final else self._round_specs()
            offered = frozenset(spec["function"]["name"] for spec in specs or ())
            self.budget.spend_model()
            calls: list = []
            text_parts: list[str] = []
            self.thinking.append("")

            self._thought = 0
            self._markup = 0
            # No ModelError handling here: `run` wraps this generator, and one
            # place that states "the model could not be reached" is enough.
            # Hold early prose if we may ask for another attempt to retrieve data.
            defer = bool(not final and specs and self.req.routing
                         and self.req.routing.needs_data and not self._has_data and not self._expanded
                         and round_no + 1 < rounds and self.budget.calls_left() > 1)
            buffered = []
            prompt = _with_plan(_window(messages, self._window_tokens, specs), self.ctx.plan)
            if self._expanded and not self._has_data and specs:
                prompt = prompt + [{"role": "user", "content": (
                    "The first attempt yielded no data. Try a different available tool or "
                    "query if you have the details. Otherwise ask or state what is unavailable."
                )}]
            if self._news_dispatched:
                prompt = prompt + [{"role": "user", "content": (
                    "Use the completed news lookup in your answer. Cite the returned news sources. "
                    "If it failed or returned no relevant results, say so explicitly."
                )}]
            async for event in self._stream_round(
                prompt, specs,
                self.req.thinking,
                round_no, calls, text_parts,
            ):
                if defer and event.type == events.MODEL_DELTA:
                    buffered.append(event)
                else:
                    yield event
            if defer and not calls and not self._news_pending():
                prose = "".join(text_parts).strip()
                clarification = len(prose) <= 180 and prose.endswith("?")
                if not clarification and self._widen_route(round_no):
                    round_no += 1
                    continue
            for event in buffered:
                yield event
            # A round can end with nothing to show: it reasoned to the end
            # and stopped, or (on a tool-less pass) it wrote a tool call
            # into the answer channel, which the server cannot parse. Neither
            # is an answer, so it is asked once more with no tools and no
            # thinking before the turn is called finished. The final pass is
            # left to its own block below, which asks for the same thing after
            # running whatever call it did make.
            stuck = not final and not calls and not text_parts and not self._news_pending() and (
                self._thought or self._markup)
            if stuck:
                async for event in self._answer_or_sorry(
                    messages, round_no, calls, text_parts
                ):
                    yield event

            def record():
                """File the round's prose as the answer. The recovery passes add
                to `text_parts` after the first model call, so this runs once
                the round is truly done rather than the moment it streams."""
                text = "".join(text_parts)
                if text:
                    self.result.answer = text
                    self.result.answers.append({"r": round_no, "text": text})

            if final:
                # The only call the final pass can still make is `plan`, and
                # dropping it would leave the card one step behind the answer.
                # A tool it was not offered is not run, whatever it named.
                offered = self._answer_names
                runnable = [c for c in calls
                            if (c.get("function") or {}).get("name") in offered]
                if runnable:
                    messages.append({"role": "assistant", "content": "", "tool_calls": runnable})
                    async for event in self._run_calls(runnable, messages, round_no):
                        yield event
                # A model that spends the answering pass on the plan card and
                # never reaches the prose leaves the turn with a card and no
                # answer, which the reader sees as the run having stopped. The
                # step is kept, and the answer is asked for once more with
                # nothing left to call.
                if not text_parts:
                    async for event in self._answer_or_sorry(
                        messages, round_no, calls, text_parts
                    ):
                        yield event
                record()
                self.result.stop_reason = "answered"
                break
            record()
            if not calls:
                if (self._news_pending() and self.budget.calls_left() > 0
                        and self.budget.spent_tools < self.budget.tool_calls):
                    async for event in self._search_requested_news(messages, round_no):
                        yield event
                    if self._chart_symbol and not self.req.routing.after_data:
                        rounds = min(rounds, round_no + 1)
                        extra = 0
                    round_no += 1
                    continue
                self.result.stop_reason = "answered"
                break

            messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            self._invalid_calls.clear()
            allowed = offered if self.req.routing is not None else None
            async for event in self._run_calls(calls, messages, round_no, allowed=allowed):
                yield event
            if self.req.routing is not None:
                if self._invalid_calls and not self._repair_used and round_no + 1 < rounds:
                    self._repair_names = tuple(name for name in self._available
                                               if name in self._invalid_calls)
                    self._repair_used = True
                else:
                    self._blocked_tools.update(self._invalid_calls)
                    self._widen_route(round_no)

            round_no += 1

    async def _stream_round(self, messages, specs, thinking, round_no, calls, text_parts):
        """One model call, its events forwarded as they arrive.

        The parts are collected into the caller's lists rather than returned,
        because a round can be streamed twice and both halves are one answer.
        What came back is counted on the engine, so the caller can tell a round
        that thought and answered nothing from one that simply had nothing to
        say, and from one that wrote a tool call into the answer channel.
        """
        markup = _ToolCallFilter()

        def prose(piece):
            text = markup.feed(piece)
            if text:
                text_parts.append(text)
            return text

        async for kind, value in stream_completion(
            messages,
            model=self.req.model,
            thinking=thinking,
            tools=specs,
            ctx=self.req.ctx_tokens,
        ):
            if kind == "tool_calls":
                merge_tool_calls(calls, value)
            elif kind == "delta":
                text = prose(value)
                if text:
                    yield emit(events.MODEL_DELTA, text=text, round=round_no)
            elif kind == "thinking":
                self._thought += len(value)
                self.thinking[-1] += value
                yield emit(events.MODEL_THINKING, text=value, round=round_no)
            elif kind == "metrics":
                # Sent on every call rather than kept: the browser shows the
                # last one it saw, and the last call is the one whose prompt
                # held the whole conversation, which is the count that
                # describes the context in use.
                if value.get("context_source") in {"reported", "runner"} and value.get("context_window"):
                    self._window_tokens = value["context_window"]
                yield emit(events.MODEL_METRICS, round=round_no, **value)

        tail = markup.flush()
        if tail:
            text_parts.append(tail)
            yield emit(events.MODEL_DELTA, text=tail, round=round_no)
        if markup.dropped:
            self._markup += markup.dropped

    def _headroom(self):
        """Extra rounds the model-call budget can afford, one call held back
        for the answer."""
        return max(self.budget.calls_left() - self.budget.rounds - 1, 0)

    async def _recover(self, messages, round_no, calls, text_parts):
        """Ask for prose once more, with no tools and no thinking.

        Budget permitting, the same conversation is sent with the no-answer
        note. A model that thought its way to a stop, or wrote a call as text,
        gets the plainest possible request before the round is called
        answerless.
        """
        if not self.budget.may_call():
            return
        self.budget.spend_model()
        async for event in self._stream_round(
            _answer_ask(_window(messages, self._window_tokens), self.ctx.plan), None, False,
            round_no, calls, text_parts,
        ):
            yield event

    async def _answer_or_sorry(self, messages, round_no, calls, text_parts):
        """Ask once more, and if that goes unanswered too, say so.

        Both call sites reach this the same way — a round that ended with no
        prose — and both need the same pair of steps: the extra ask, then the
        plain sentence when even that produces nothing, because a reader shown
        an empty turn reads it as the run having stopped.
        """
        async for event in self._recover(messages, round_no, calls, text_parts):
            yield event
        if not text_parts:
            text_parts.append(_NO_ANSWER_SORRY)
            yield emit(
                events.MODEL_DELTA, text=_NO_ANSWER_SORRY, round=round_no)

    async def _run_calls(self, calls, messages, round_no, *, allowed=None):
        """Run one round's calls together, reporting each as it finishes.

        The tools run concurrently because the calls in a round are
        independent: three fetches the model asked for at once should cost the
        slowest one, not the sum. Their results are reported the moment they
        arrive, so a fast call's card closes while a slow one is still running
        rather than both appearing together when the round ends.

        Two things stay in the order the model asked, because arrival order is
        not something either of them can use. Budget is spent before anything
        starts, and the messages the model reads are appended after the gather:
        a model handed its results in completion order would answer its own
        calls out of sequence on the next round. The browser places a card by
        its round and does not need them ordered, so events are free to follow
        completion.
        """
        started = []
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name")
            # Parsed here as well as in `Registry.call`, because the started
            # event carries the arguments and is emitted before anything runs.
            args = as_args(fn.get("arguments"))

            yield emit(events.TOOL_STARTED, tool=name, args=args,
                       id=call_id(call), round=round_no)

            try:
                if self._news_pending() and self.budget.spent_tools == self.budget.tool_calls - 1:
                    news_call = (name == WEB_SEARCH and (allowed is None or name in allowed)
                                 and is_news_lookup(self.req.routing, args))
                    if not news_call:
                        raise BudgetExceeded("tool_calls", self.budget.tool_calls, self.budget.spent_tools)
                self.budget.spend_tool()
            except BudgetExceeded as e:
                yield emit(
                    events.TOOL_FAILED, tool=name, round=round_no, args=args,
                    id=call_id(call), code=errors.BUDGET_EXCEEDED, message=str(e),
                )
                started.append((call, name, args, None, e))
                continue
            started.append((call, name, args, self._call_with_retry(name, args, allowed), None))

        results = {}

        async def tagged(index, coro):
            # The completion has to name which call it belongs to, because
            # `as_completed` answers in the order things finished.
            return index, await coro

        running = [tagged(i, item[3]) for i, item in enumerate(started)
                   if item[3] is not None]
        for finished in asyncio.as_completed(running):
            index, result = await finished
            results[index] = result
            for event in self._report(round_no, started[index], result):
                yield event

        for i, (call, name, args, job, err) in enumerate(started):
            if job is None:
                messages.append(tool_message(call, f"{name} was not run: {err}"))
                continue
            result = results[i]
            text = result.text if result.text is not None else "(no result)"
            messages.append(tool_message(call, clip(text, self.budget.tool_output_chars)))

    def _report(self, round_no, entry, result):
        """The events one finished call produces, in the order they must be read.

        Split out so the loop above states only when a call is reported, and
        every event a result becomes is stated once, here.
        """
        call, name, args, _, _ = entry
        text = result.text if result.text is not None else "(no result)"
        text = clip(text, self.budget.tool_output_chars)

        if self.req.routing is not None:
            tool = registry.get(name)
            code = (result.error or {}).get("code")
            if name == WEB_SEARCH and is_news_lookup(self.req.routing, args) and code not in {
                errors.INVALID_ARGUMENTS, errors.TOOL_NOT_OFFERED,
            }:
                self._news_attempted = True
            card = (result.metadata or {}).get("card")
            if result.success and isinstance(card, dict) and card.get("candles"):
                self._chart_symbol = card.get("symbol") or ""
            if result.success and tool is not None and not tool.last_pass:
                self._has_data |= provides_data(name, result)
                self._found_links |= links_from_search(name, result)
            elif code == errors.INVALID_ARGUMENTS:
                if tool is not None and tool.permission in ALLOWED:
                    self._invalid_calls.add(name)
                else:
                    self._blocked_tools.add(name)

        for source in (result.metadata or {}).get("sources", []) or []:
            # A tool that wrote citation numbers into its own text reserved
            # its block from the run's counter and stamps the rows. Any
            # other source is numbered here, so every row the browser sees
            # has the `n` its links need.
            if not source.get("n"):
                source = {**source, "n": self.ctx.numbers.reserve()}
            self.result.sources.append(source)
            if self._note_source(source):
                self._new_sources += 1
            # Every row a call produced belongs to that call, so the id is on
            # all of them: two searches in one round finish in either order,
            # and the round alone would file the second one's results under
            # the first one's card.
            yield emit(events.SOURCE_FOUND, round=round_no,
                       id=call_id(call), **source)

        if result.success:
            # `card` is the tool's own figures for the browser to draw, and
            # only the tools that publish one carry it; everything else
            # reaches the cards through sources.
            #
            # `id` rides along because a round's calls finish in completion
            # order, so the round alone cannot say which of two fetches this
            # closing event belongs to. The id the model gave the call is the
            # one thing that can, and it is the same id the tool message
            # carries, so the card and the stored turn agree on the call.
            yield emit(
                events.TOOL_COMPLETED, tool=name, round=round_no, args=args,
                id=call_id(call), chars=len(text or ""),
                sources=len((result.metadata or {}).get("sources", []) or []),
                card=(result.metadata or {}).get("card"),
            )
        else:
            # `reason` is the tool's own short phrase for what stopped it,
            # which is what the card replays; `message` is the sentence the
            # model was given, which repeats the URL the card already shows.
            yield emit(
                events.TOOL_FAILED, tool=name, round=round_no, args=args,
                id=call_id(call),
                code=(result.error or {}).get("code", errors.TOOL_ERROR),
                message=(result.error or {}).get("message", ""),
                reason=str((result.metadata or {}).get("reason", "") or ""),
            )

    def _note_source(self, source):
        """True if this source is new to the run."""
        key = source.get("source") or f"{source.get('kind', '')}:{source.get('title', '')}"
        if not key or key in self._seen_sources:
            return False
        self._seen_sources.add(key)
        return True

    async def _call_with_retry(self, name, args, allowed=None):
        """Run a tool, retrying calls that cannot have changed anything.

        A write or a command isn't retried: the runtime can't tell if the
        first attempt took effect, and repeating could double the side
        effect. The test is the declared permission, not `authorize`: a
        writing tool may be authorized and still be unsafe to repeat, so
        asking "may this run" would answer the wrong question.
        """
        tool = registry.get(name)
        if allowed is not None and tool is not None and name not in allowed:
            return ToolResult.failed(
                errors.TOOL_NOT_OFFERED,
                f"{name} was not offered this round. Use an offered tool or answer without it.",
                reason="tool was not offered",
            )
        result = await registry.call(name, args, self.ctx)
        if result.success:
            return result
        if allowed is not None and (result.error or {}).get("code") == errors.INVALID_ARGUMENTS:
            message = result.error["message"]
            hint = (" Correct it once using the request, or ask for the missing detail."
                    if tool is not None and tool.permission in ALLOWED and not self._repair_used
                    else "")
            return ToolResult.failed(errors.INVALID_ARGUMENTS, f"{message}{hint}", reason=message)
        retryable = bool((result.error or {}).get("retryable"))
        safe = tool is not None and tool.permission in ALLOWED
        if retryable and safe and self.budget.allow_retry():
            return await registry.call(name, args, self.ctx)
        return result

