"""The one execution loop.

The loop calls the model, runs the tools it requested, feeds results back, and
stops when the model answers, the budget runs out, or a limit is reached.

It does not decide permissions (the tool registry does), does not know tools
by name (registration does), and does not write to the database directly.
Everything is reported as an event with a sequence number for replay.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..models import ModelError, cached_capabilities, merge_tool_calls, stream_completion
from ..security.permissions import EXTERNAL, READ_ONLY
from ..tools.registry import ToolContext, registry
from . import errors, events
from .budgets import Budget, BudgetExceeded, clip
from .events import Emitter, scrub

MAX_TOOL_MESSAGES = 60


@dataclass
class RunRequest:
    """One question and the conditions it runs under."""

    messages: list                       # [{role, content}] with system prompt first
    model: str | None = None
    thinking: bool | str = False         # a rung the model accepts: False, True, or a level
    ctx_tokens: int | None = None
    tags: tuple = ("chat",)              # which tools to offer
    budget: Budget | None = None
    tool_context: ToolContext | None = None
    system_prompt: str | None = None


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
    tool_runs: list = field(default_factory=list)
    model_calls: int = 0
    rounds: int = 0
    stop_reason: str = "answered"
    budget: dict = field(default_factory=dict)
    error: str | None = None
    metrics: dict = field(default_factory=dict)   # what the last model call cost

    def as_dict(self):
        return {
            "answer": self.answer,
            "sources": self.sources,
            "tool_runs": self.tool_runs,
            "model_calls": self.model_calls,
            "rounds": self.rounds,
            "stop_reason": self.stop_reason,
            "budget": self.budget,
            "error": self.error,
            "metrics": self.metrics,
        }


def tool_message(call, content):
    """The message that carries a tool's output back to the model."""
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
        lines.append(f"{i}. [{s['status']}] {s['step']}")
    lines.append("Keep going through it, and call plan again when a step changes.")
    return messages + [{"role": "user", "content": "\n".join(lines)}]


_OPEN = "<tool_call>"
_CLOSE = "</tool_call>"

# Sent when a pass leaves nothing but a tool call written as text. The server
# only parses calls when the request carries the tool list, so a tool-less pass
# can turn one into prose; the answer has to be asked for again, plainly.
_AS_TEXT = (
    "That reply was a tool call written as text, so the reader got markup "
    "instead of an answer. Answer in prose now, without any tool call and "
    "without <tool_call> tags."
)

# Sent when the final pass spent itself on a `plan` call and left no prose. The
# step is recorded and its card is kept, but a card is not an answer.
_AFTER_PLAN = (
    "You recorded the plan but wrote no answer, so the reader has a card and "
    "nothing else. Answer in prose now, from what the run already found, and "
    "do not call any tool."
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


def _answer_ask(messages, plan, note=_AS_TEXT):
    """The conversation plus the note that sends the model back to prose."""
    return _with_plan(messages, plan) + [{"role": "user", "content": note}]


def _drop_old_tool_messages(messages):
    """Keep the conversation bounded without losing the turns that matter: the
    system prompt, the first user turn, and the newest tool exchanges stay."""
    if len(messages) <= MAX_TOOL_MESSAGES:
        return messages
    head = messages[:2]
    tail = messages[-(MAX_TOOL_MESSAGES - 2):]
    # The cut can land inside a round. A `tool` message whose parent assistant
    # call went over the edge is an orphan, and a server that pairs them by id
    # rejects the whole request rather than the one message.
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    return head + tail


class Engine:
    """Runs one request and yields its events."""

    def __init__(self, request: RunRequest, emitter: Emitter | None = None):
        self.req = request
        self.emitter = emitter or Emitter()
        self.budget = request.budget or Budget()
        self.ctx = request.tool_context or ToolContext()
        self.result = RunResult()
        self.thinking: list[str] = []
        self.sources: list[dict] = []
        # Track which sources the run has already seen, to know when a
        # repeated search finds nothing new and should stop.
        self._seen_sources: set = set()
        self._new_sources = 0
        self._cancelled = False
        self._allowed_tool_names = {s["function"]["name"] for s in registry.specs_for(self.ctx)}
        # The offered list is the same every round: the registry is fixed for the
        # life of a run, and only the final pass withholds it.
        #
        # A model that declares it does not call tools is offered none: the
        # list is sent on every round, and one the model cannot use is prompt
        # spent on a schema it will not answer. An undeclared model keeps the
        # list, because nothing was said and a silent model is not a capable
        # one — `None` means "unknown" here, never "no".
        declared = cached_capabilities(request.model)
        offer = declared is None or declared.get("tools", True)
        specs = [s for s in registry.specs(request.tags)
                 if s["function"]["name"] in self._allowed_tool_names]
        self._specs = specs if offer and specs else None
        # The final pass keeps `plan` and drops the rest. A model that still
        # wants to record a step can call it; one that wants to keep searching
        # has no schema to call, so the search ends where the budget says.
        # Without any list at all a model reaches for the tool anyway and
        # writes the call into the answer channel, which the reader then sees
        # as markup.
        self._answer_specs = None
        if offer:
            self._answer_specs = [s for s in specs
                                  if s["function"]["name"] == "plan"] or None

    def cancel(self):
        """Ask the loop to stop at the next boundary. A tool already running is
        not interrupted mid-write; the check happens between steps, which is
        where a stop is safe."""
        self._cancelled = True

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
            self.result.error = str(e)
            yield self.emitter.emit(events.TASK_FAILED, reason="budget", **e.as_dict())
        except ModelError as e:
            self.result.stop_reason = "model_error"
            self.result.error = str(e)
            yield self.emitter.emit(
                events.TASK_FAILED, reason="model_error", code=errors.MODEL_ERROR, message=str(e)
            )
        self.result.budget = self.budget.used()
        self.result.sources = self.sources
        # One entry per round, empty where a round thought nothing. The browser
        # places a card by its round number, so dropping the empty slots would
        # shift every later round's thinking onto an earlier round's cards.
        self.result.thinking = list(self.thinking)

    async def _loop(self):
        messages = _drop_old_tool_messages(list(self.req.messages))

        rounds = self.budget.rounds
        # Extra rounds only if the model-call budget can pay for them.
        extra = min(self.budget.extra_rounds, self._headroom(rounds))
        round_no = 0
        while True:
            if self._cancelled:
                self.result.stop_reason = "cancelled"
                yield self.emitter.emit(events.TASK_CANCELLED)
                return

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
            self.result.model_calls += 1

            yield self.emitter.emit(
                events.MODEL_STARTED, round=round_no, final=final, tools=bool(specs)
            )
            calls: list = []
            text_parts: list[str] = []
            self.thinking.append("")

            # Thinking off on the final pass: a reasoning model can keep
            # reasoning to the token limit with no answer, which the user
            # sees as an answer hidden in a thinking card. Every other pass
            # carries the rung exactly as it was resolved, which may be
            # `True`, `False`, or a level name the model itself accepts.
            seen = {}
            try:
                async for event in self._stream_round(
                    _with_plan(messages, self.ctx.plan), specs,
                    False if final else self.req.thinking,
                    round_no, calls, text_parts, seen,
                ):
                    yield event
                # A round that reasons its way to the end and then stops without
                # a word leaves the turn with nothing to show. The same request
                # with the tool list dropped and the thinking channel closed is
                # what gets the prose out, so it is asked once more before the
                # turn is called finished. The list goes too: a tool round keeps
                # the lowest thinking rung on models where `false` would take the
                # tools with it, and this pass exists to have no thinking at all.
                if (
                    not final
                    and not calls
                    and not text_parts
                    and seen.get("thought")
                    and self.budget.spent_models < self.budget.model_calls
                ):
                    self.budget.spend_model()
                    self.result.model_calls += 1
                    async for event in self._stream_round(
                        _with_plan(messages, self.ctx.plan), None, False,
                        round_no, calls, text_parts, seen
                    ):
                        yield event
                # A pass can end with a tool call written as text and no prose.
                # The server only parses calls when the request carries the
                # list, so this is what a tool-less pass does with a model that
                # still wants to act. Markup is not an answer: the turn is asked
                # once more, plainly, with nothing to call.
                if (
                    not calls
                    and not text_parts
                    and seen.get("markup")
                    and self.budget.spent_models < self.budget.model_calls
                ):
                    self.budget.spend_model()
                    self.result.model_calls += 1
                    async for event in self._stream_round(
                        _answer_ask(messages, self.ctx.plan), None, False,
                        round_no, calls, text_parts, seen,
                    ):
                        yield event
                # The ask did not take either. The round has no answer to show,
                # and the markup it did produce is not one, so the reader is
                # told what happened instead of being handed the raw markup.
                # This fires on any round: a model that only writes markup made
                # no calls, so the turn ends here whatever the budget says.
                if not calls and not text_parts and seen.get("markup"):
                    sorry = ("The model answered with a tool call instead of an "
                             "answer, and said nothing when asked again.")
                    text_parts.append(sorry)
                    yield self.emitter.emit(events.MODEL_DELTA, text=sorry, round=round_no)
            except ModelError as e:
                yield self.emitter.emit(
                    events.TASK_FAILED, reason="model_error", code=errors.MODEL_ERROR, message=str(e)
                )
                self.result.stop_reason = "model_error"
                self.result.error = str(e)
                return

            yield self.emitter.emit(
                events.MODEL_COMPLETED,
                round=round_no,
                chars=sum(len(p) for p in text_parts),
                tool_calls=len(calls),
            )
            self.result.rounds = round_no + 1

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
                offered = {s["function"]["name"] for s in (self._answer_specs or [])}
                runnable = [c for c in calls
                            if (c.get("function") or {}).get("name") in offered]
                runnable = self._filter_calls(runnable)
                if runnable:
                    messages.append({"role": "assistant", "content": "", "tool_calls": runnable})
                    async for event in self._run_calls(runnable, messages, round_no):
                        yield event
                # A model that spends the answering pass on the plan card and
                # never reaches the prose leaves the turn with a card and no
                # answer, which the reader sees as the run having stopped. The
                # step is kept, and the answer is asked for once more with
                # nothing left to call.
                if (not text_parts
                        and self.budget.spent_models < self.budget.model_calls):
                    self.budget.spend_model()
                    self.result.model_calls += 1
                    async for event in self._stream_round(
                        _answer_ask(messages, self.ctx.plan, _AFTER_PLAN), None, False,
                        round_no, calls, text_parts, seen,
                    ):
                        yield event
                if not text_parts:
                    # The ask did not take. Nothing was answered, and the reader
                    # is told that rather than left with the card alone.
                    sorry = ("The model recorded its plan but never wrote an "
                             "answer, and said nothing when asked again.")
                    text_parts.append(sorry)
                    yield self.emitter.emit(events.MODEL_DELTA, text=sorry, round=round_no)
                record()
                self.result.stop_reason = "answered"
                break
            record()
            if not calls:
                self.result.stop_reason = "answered"
                break
            if self._cancelled:
                self.result.stop_reason = "cancelled"
                yield self.emitter.emit(events.TASK_CANCELLED)
                return

            runnable = self._filter_calls(calls)
            if not runnable:
                self.result.stop_reason = "no_valid_calls"
                break

            messages.append({"role": "assistant", "content": "", "tool_calls": runnable})
            async for event in self._run_calls(runnable, messages, round_no):
                yield event

            round_no += 1

        yield self.emitter.emit(events.TASK_COMPLETED, **self.result.as_dict())

    async def _stream_round(self, messages, specs, thinking, round_no, calls, text_parts, seen):
        """One model call, its events forwarded as they arrive.

        The parts are collected into the caller's lists rather than returned,
        because a round can be streamed twice and both halves are one answer.
        `seen` records what came back, so the caller can tell a round that
        thought and answered nothing from one that simply had nothing to say,
        and from one that wrote a tool call into the answer channel.
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
                    yield self.emitter.emit(events.MODEL_DELTA, text=text, round=round_no)
            elif kind == "thinking":
                seen["thought"] = seen.get("thought", 0) + len(value)
                self.thinking[-1] += value
                yield self.emitter.emit(events.MODEL_THINKING, text=value, round=round_no)
            elif kind == "metrics":
                # Kept per round and overwritten, because the last call is the
                # one whose prompt held the whole conversation: it is the only
                # one whose count describes the context in use, and that is
                # what a reader wants to see.
                self.result.metrics = value
                yield self.emitter.emit(events.MODEL_METRICS, round=round_no, **value)

        tail = markup.flush()
        if tail:
            text_parts.append(tail)
            yield self.emitter.emit(events.MODEL_DELTA, text=tail, round=round_no)
        if markup.dropped:
            seen["markup"] = seen.get("markup", 0) + markup.dropped

    def _headroom(self, rounds):
        """Extra rounds the model-call budget can afford."""
        left = self.budget.model_calls - self.budget.spent_models
        return max(left - rounds - 1, 0)

    def _filter_calls(self, calls):
        """Drop names nobody registered, and keep the rest in order."""
        out = []
        for call in calls:
            name = (call.get("function") or {}).get("name")
            if registry.get(name) is None:
                self.result.tool_runs.append(
                    {"tool": name or "?", "ok": False, "error": {"code": errors.UNKNOWN_TOOL,
                                                                 "message": f"no tool named {name!r}"}}
                )
                continue
            out.append(call)
        return out

    async def _run_calls(self, calls, messages, round_no=0):
        """Run one round's calls together, then report them in the order asked.

        The tools run concurrently because the calls in a round are
        independent: three fetches the model asked for at once should cost the
        slowest one, not the sum. Everything that has to stay ordered does:
        budget is spent in call order, the messages the model reads follow the
        order it asked in, and the events are emitted the same way, because the
        browser places a card by its round and closes the oldest first.
        """
        if self._cancelled:
            yield self.emitter.emit(events.TASK_CANCELLED)
            return

        started = []
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            yield self.emitter.emit(events.TOOL_STARTED, tool=name, args=args, round=round_no)

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
                yield self.emitter.emit(
                    events.TOOL_FAILED, tool=name, round=round_no,
                    code=errors.BUDGET_EXCEEDED, message=str(err),
                )
                messages.append(tool_message(call, f"{name} was not run: {err}"))
                continue

            result = results[i]
            run = {
                "tool": name,
                "args": scrub(args),
                "ok": result.success,
                "error": result.error,
            }
            self.result.tool_runs.append(run)

            text = result.text if result.text is not None else _render(result.data)
            text = clip(text, self.budget.tool_output_chars)
            messages.append(tool_message(call, text))

            for source in (result.metadata or {}).get("sources", []) or []:
                self.sources.append(source)
                if self._note_source(source):
                    self._new_sources += 1
                yield self.emitter.emit(events.SOURCE_FOUND, round=round_no, **source)

            if result.success:
                # `card` is the tool's own figures for the browser to draw, and
                # only the tools that publish one carry it; everything else
                # reaches the cards through sources.
                yield self.emitter.emit(
                    events.TOOL_COMPLETED, tool=name, round=round_no, chars=len(text or ""),
                    sources=len((result.metadata or {}).get("sources", []) or []),
                    card=(result.metadata or {}).get("card"),
                )
            else:
                # `reason` is the tool's own short phrase for what stopped it,
                # which is what the card replays; `message` is the sentence the
                # model was given, which repeats the URL the card already shows.
                yield self.emitter.emit(
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
        effect. `EXTERNAL` covers the network reads, which are idempotent
        when the tool says so.
        """
        result = await registry.call(name, args, self.ctx)
        if result.success:
            return result
        tool = registry.get(name)
        retryable = bool((result.error or {}).get("retryable"))
        safe = tool is not None and tool.permission in (READ_ONLY, EXTERNAL)
        if retryable and safe and self.budget.allow_retry():
            return await registry.call(name, args, self.ctx)
        return result


def _render(data):
    if not data:
        return "(no result)"
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(data)