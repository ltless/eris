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
from ..security import authorize
from ..tools.plan import step_line
from ..tools.registry import TAGS, ToolContext, as_args, registry
from . import events
from .budgets import Budget, BudgetExceeded, clip
from .context import recent
from .events import emit


@dataclass
class RunRequest:
    """One question and the conditions it runs under."""

    messages: list                       # [{role, content}] with system prompt first
    model: str | None = None
    thinking: bool | str = False         # a rung the model accepts: False, True, or a level
    ctx_tokens: int | None = None
    tags: tuple = TAGS                   # which tools to offer
    budget: Budget | None = None
    tool_context: ToolContext | None = None


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
        if self._inside:
            self._buf = ""
            self._inside = False
            return ""
        keep = _partial_prefix(self._buf)
        rest = self._buf[:-keep] if keep else self._buf
        self._buf = ""
        self._out += len(rest)
        return rest


def _answer_ask(messages, plan):
    """The conversation plus the note that sends the model back to prose."""
    return _with_plan(messages, plan) + [{"role": "user", "content": _NO_ANSWER}]


def _window(messages):
    """The conversation as the model gets it: the system prompt, then the newest
    turns that fit. A round appends a pair of messages per call, so the window
    is recomputed each round rather than decided once at the door."""
    if len(messages) <= 1:
        return messages
    return messages[:1] + recent(messages[1:])


class Engine:
    """Runs one request and yields its events."""

    def __init__(self, request: RunRequest):
        self.req = request
        self.budget = request.budget or Budget()
        self.ctx = request.tool_context or ToolContext()
        self.result = RunResult()
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
        # The offered list is the same every round: the registry is fixed for the
        # life of a run, and only the final pass withholds it. Both lists come
        # from one pass over the registry, so the tools a run may use are
        # decided in one place.
        #
        # A model that declares it does not call tools is offered none: the
        # list is sent on every round, and one the model cannot use is prompt
        # spent on a schema it will not answer. An undeclared model keeps the
        # list, because nothing was said and a silent model is not a capable
        # one — `None` means "unknown" here, never "no".
        declared = cached_capabilities(request.model)
        offer = declared is None or declared.get("tools", True)
        specs = registry.specs_for(self.ctx, request.tags) if offer else []
        self._specs = specs or None
        # The final pass keeps the tools that declared `last_pass` and drops the
        # rest. A model that still wants to record a step can call it; one that
        # wants to keep searching has no schema to call, so the search ends
        # where the budget says. Without any list at all a model reaches for the
        # tool anyway and writes the call into the answer channel, which the
        # reader then sees as markup.
        kept = {t.name for t in registry.all() if t.last_pass}
        answer_specs = [s for s in specs if s["function"]["name"] in kept]
        # The names the answering pass offers, and the list of schemas it offers,
        # are the same set: the final pass runs a call only if it was offered, so
        # both are built here from one pass over the registry.
        self._answer_names = frozenset(s["function"]["name"] for s in answer_specs)
        self._answer_specs = answer_specs or None

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

            # The final pass keeps `plan` and drops the busier tools, so the
            # model answers from what it has rather than searching until the
            # budget dies.
            specs = self._answer_specs if final else self._specs
            self.budget.spend_model()
            calls: list = []
            text_parts: list[str] = []
            self.thinking.append("")

            # Thinking off on the final pass: a reasoning model can keep
            # reasoning to the token limit with no answer, which the user
            # sees as an answer hidden in a thinking card. Every other pass
            # carries the rung exactly as it was resolved, which may be
            # `True`, `False`, or a level name the model itself accepts.
            self._thought = 0
            self._markup = 0
            # No ModelError handling here: `run` wraps this generator, and one
            # place that states "the model could not be reached" is enough.
            async for event in self._stream_round(
                _with_plan(_window(messages), self.ctx.plan), specs,
                False if final else self.req.thinking,
                round_no, calls, text_parts,
            ):
                yield event
            # A round can end with nothing to show: it reasoned to the end
            # and stopped, or (on a tool-less pass) it wrote a tool call
            # into the answer channel, which the server cannot parse. Neither
            # is an answer, so it is asked once more with no tools and no
            # thinking before the turn is called finished. The final pass is
            # left to its own block below, which asks for the same thing after
            # running whatever call it did make.
            stuck = not final and not calls and not text_parts and (
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
                self.result.stop_reason = "answered"
                break

            messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            async for event in self._run_calls(calls, messages, round_no):
                yield event

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
            _answer_ask(_window(messages), self.ctx.plan), None, False,
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

    async def _run_calls(self, calls, messages, round_no):
        """Run one round's calls together, then report them in the order asked.

        The tools run concurrently because the calls in a round are
        independent: three fetches the model asked for at once should cost the
        slowest one, not the sum. Everything that has to stay ordered does:
        budget is spent in call order, the messages the model reads follow the
        order it asked in, and the events are emitted the same way, because the
        browser places a card by its round and closes the oldest first.
        """
        started = []
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name")
            # Parsed here as well as in `Registry.call`, because the started
            # event carries the arguments and is emitted before anything runs.
            args = as_args(fn.get("arguments"))

            yield emit(events.TOOL_STARTED, tool=name, args=args, round=round_no)

            try:
                self.budget.spend_tool()
            except BudgetExceeded as e:
                started.append((call, name, args, None, e))
                continue
            started.append((call, name, args, self._call_with_retry(name, args), None))

        running = [(i, item[3]) for i, item in enumerate(started) if item[3] is not None]
        done = await asyncio.gather(*(job for _, job in running)) if running else []
        results = {i: r for (i, _), r in zip(running, done)}

        for i, (call, name, args, job, err) in enumerate(started):
            if job is None:
                yield emit(
                    events.TOOL_FAILED, tool=name, round=round_no,
                    code=errors.BUDGET_EXCEEDED, message=str(err),
                )
                messages.append(tool_message(call, f"{name} was not run: {err}"))
                continue

            result = results[i]
            text = result.text if result.text is not None else "(no result)"
            text = clip(text, self.budget.tool_output_chars)
            messages.append(tool_message(call, text))

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
                yield emit(events.SOURCE_FOUND, round=round_no, **source)

            if result.success:
                # `card` is the tool's own figures for the browser to draw, and
                # only the tools that publish one carry it; everything else
                # reaches the cards through sources.
                yield emit(
                    events.TOOL_COMPLETED, tool=name, round=round_no, chars=len(text or ""),
                    sources=len((result.metadata or {}).get("sources", []) or []),
                    card=(result.metadata or {}).get("card"),
                )
            else:
                # `reason` is the tool's own short phrase for what stopped it,
                # which is what the card replays; `message` is the sentence the
                # model was given, which repeats the URL the card already shows.
                yield emit(
                    events.TOOL_FAILED, tool=name, round=round_no,
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

    async def _call_with_retry(self, name, args):
        """Run a tool, retrying calls that cannot have changed anything.

        A write or a command isn't retried: the runtime can't tell if the
        first attempt took effect, and repeating could double the side
        effect. The question is the same one the registry asks before running
        anything, so it is put to `authorize` rather than re-derived here — a
        refinement there would otherwise not reach a retry.
        """
        result = await registry.call(name, args, self.ctx)
        if result.success:
            return result
        tool = registry.get(name)
        retryable = bool((result.error or {}).get("retryable"))
        safe = tool is not None and authorize(tool) is None
        if retryable and safe and self.budget.allow_retry():
            return await registry.call(name, args, self.ctx)
        return result

