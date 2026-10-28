"""The one execution loop.

The loop calls the model, runs the tools it requested, feeds results back, and
stops when the model answers, the budget runs out, or a limit is reached.

It does not decide permissions (the tool registry does), does not know tools
by name (registration does), and does not write to the database directly.
Everything is reported as an event with a sequence number for replay.
"""

import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..models import ModelError, stream_completion
from ..security.permissions import READ_ONLY
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
    thinking: bool = False
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
    sources: list = field(default_factory=list)
    tool_runs: list = field(default_factory=list)
    model_calls: int = 0
    rounds: int = 0
    stop_reason: str = "answered"
    budget: dict = field(default_factory=dict)
    error: str | None = None

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
        }


def tool_message(call, content):
    """The message that carries a tool's output back to the model."""
    m = {"role": "tool", "content": content}
    if call.get("id"):
        m["tool_call_id"] = call["id"]
    return m


def _merge_calls(calls, incoming):
    """A round can carry several tool calls, and a server that re-sends one as
    its arguments stream in must not have it run twice."""
    for call in incoming:
        for i, seen in enumerate(calls):
            if call.get("id") and seen.get("id") == call["id"]:
                calls[i] = call
                break
        else:
            calls.append(call)


def _drop_old_tool_messages(messages):
    """Keep the conversation bounded without losing the turns that matter.

    The system prompt, the original user turn, and the newest tool exchanges
    stay; the middle of a long tool history is what goes.
    """
    if len(messages) <= MAX_TOOL_MESSAGES:
        return messages
    head = [m for m in messages[:2]]
    tail = messages[-(MAX_TOOL_MESSAGES - 2):]
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
        self._awaiting_approval = False
        self._allowed_tool_names = {s["function"]["name"] for s in registry.specs_for(self.ctx)}

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
        self.result.thinking = [t for t in self.thinking if t.strip()]

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

            # The final pass drops the tool list so the model answers from
            # what it has rather than searching until the budget dies.
            specs = None if final else self._allowed_specs(registry.specs(self.req.tags))
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
            # sees as an answer hidden in a thinking card.
            try:
                async for kind, value in stream_completion(
                    messages,
                    model=self.req.model,
                    thinking=self.req.thinking and not final,
                    tools=specs,
                    ctx=self.req.ctx_tokens,
                ):
                    if kind == "tool_calls":
                        _merge_calls(calls, value)
                    elif kind == "delta":
                        text_parts.append(value)
                        yield self.emitter.emit(events.MODEL_DELTA, text=value, round=round_no)
                    elif kind == "thinking":
                        self.thinking[-1] += value
                        yield self.emitter.emit(events.MODEL_THINKING, text=value, round=round_no)
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
            text = "".join(text_parts)
            if text:
                self.result.answer = text

            if final:
                self.result.stop_reason = "answered"
                break
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
            self.thinking.append("")
            async for event in self._run_calls(runnable, messages, round_no):
                yield event

            # A call needing approval ends the run: carrying on would let the
            # final pass overwrite `approval_required` with a plain answer,
            # hiding what the model wanted to do from the user.
            if self._awaiting_approval:
                self.result.stop_reason = "approval_required"
                break

            round_no += 1

        yield self.emitter.emit(events.TASK_COMPLETED, **self.result.as_dict())

    def _headroom(self, rounds):
        """Extra rounds the model-call budget can afford."""
        left = self.budget.model_calls - self.budget.spent_models
        return max(left - rounds - 1, 0)

    def _allowed_specs(self, specs):
        """Withhold tools the context can't use; offer those needing approval."""
        out = [s for s in specs or [] if s["function"]["name"] in self._allowed_tool_names]
        return out or None

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
        for call in calls:
            if self._cancelled:
                yield self.emitter.emit(events.TASK_CANCELLED)
                return
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
                yield self.emitter.emit(
                    events.TOOL_FAILED, tool=name, round=round_no,
                    code=errors.BUDGET_EXCEEDED, message=str(e),
                )
                messages.append(tool_message(call, f"{name} was not run: {e}"))
                continue

            result = await self._call_with_retry(name, args)
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
                yield self.emitter.emit(
                    events.TOOL_COMPLETED, tool=name, round=round_no, chars=len(text or ""),
                    sources=len((result.metadata or {}).get("sources", []) or []),
                )
            elif result.error and result.error.get("code") == "approval_required":
                self._awaiting_approval = True
                yield self.emitter.emit(
                    events.APPROVAL_REQUIRED, tool=name, round=round_no, args=args,
                    message=result.error["message"],
                )
                self.result.stop_reason = "approval_required"
            else:
                yield self.emitter.emit(
                    events.TOOL_FAILED, tool=name, round=round_no,
                    code=(result.error or {}).get("code", errors.TOOL_ERROR),
                    message=(result.error or {}).get("message", ""),
                )

    def _note_source(self, source):
        """True if this source is new to the run."""
        key = source.get("source") or f"{source.get('kind', '')}:{source.get('title', '')}"
        if not key or key in self._seen_sources:
            return False
        self._seen_sources.add(key)
        return True

    async def _call_with_retry(self, name, args):
        """Run a tool, retrying read-only failures only.

        Writes and commands aren't retried: the runtime can't tell if the
        first attempt took effect, and repeating could double the side effect.
        """
        result = await registry.call(name, args, self.ctx)
        if result.success:
            return result
        tool = registry.get(name)
        retryable = bool((result.error or {}).get("retryable"))
        safe = tool is not None and tool.permission == READ_ONLY
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