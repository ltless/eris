"""Execution limits, enforced in code rather than described to the model.

A model that is told "search at most three times" searches as long as it likes.
These counters are checked before each call, and the run ends with a stated
reason when one is reached.
"""

import time
from dataclasses import dataclass, field

from ..config import settings


class BudgetExceeded(Exception):
    """Raised when a limit is hit. `kind` names the limit that stopped the run."""

    def __init__(self, kind, limit, spent):
        super().__init__(f"{kind} budget exhausted ({spent}/{limit})")
        self.kind = kind


@dataclass
class Budget:
    """Per-run limits.

    `rounds` is the soft tool-round allowance: the model gets that many rounds
    plus one more call with no tools, so a turn ends in an answer. A round that
    brought back a source the run had not seen before may earn one of
    `extra_rounds` beyond that, which is how a hard question gets searched
    further while a question that is already answered stops. The model-call
    limit is the real ceiling: one call is always held back for the answer.
    """

    rounds: int = settings.max_tool_rounds
    extra_rounds: int = settings.extra_tool_rounds
    model_calls: int = 24
    tool_calls: int = 48
    seconds: float = 900.0
    tool_output_chars: int = settings.tool_output_chars
    retries: int = 1
    started: float = field(default_factory=time.monotonic)
    spent_models: int = 0
    spent_tools: int = 0
    retries_used: int = 0

    def elapsed(self):
        return time.monotonic() - self.started

    def check_time(self):
        if self.elapsed() >= self.seconds:
            raise BudgetExceeded("seconds", self.seconds, round(self.elapsed(), 1))

    def calls_left(self):
        """Model calls still affordable, never below zero."""
        return max(self.model_calls - self.spent_models, 0)

    def may_call(self):
        """Whether one more model call fits. The question `spend_model` asks,
        put plainly, so a caller that wants to know before spending does not
        re-derive the comparison."""
        return self.spent_models < self.model_calls

    def spend_model(self):
        self.check_time()
        if not self.may_call():
            raise BudgetExceeded("model_calls", self.model_calls, self.spent_models)
        self.spent_models += 1

    def spend_tool(self):
        self.check_time()
        if self.spent_tools >= self.tool_calls:
            raise BudgetExceeded("tool_calls", self.tool_calls, self.spent_tools)
        self.spent_tools += 1

    def allow_retry(self):
        """One retry per failed read-only call, and only while retries remain."""
        if self.retries_used >= self.retries:
            return False
        self.retries_used += 1
        return True

    def used(self):
        return {
            "model_calls": self.spent_models,
            "tool_calls": self.spent_tools,
            "seconds": round(self.elapsed(), 2),
            "retries": self.retries_used,
        }


def clip(text, limit):
    """Bound what a tool hands back to the model."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} characters omitted]"