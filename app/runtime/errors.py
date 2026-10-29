"""Failures the runtime can name.

A tool failure reaches the model as a tool message and the browser as an event,
so both need to be told what went wrong. An exception's class name is not that
answer, so each one is normalized to a code, a message, and whether retrying
the same call could plausibly work.
"""

from dataclasses import dataclass

# Codes the runtime emits. The frontend switches on these.
UNKNOWN_TOOL = "unknown_tool"
INVALID_ARGUMENTS = "invalid_arguments"
TOOL_ERROR = "tool_error"
TIMEOUT = "timeout"
APPROVAL_REQUIRED = "approval_required"
BUDGET_EXCEEDED = "budget_exceeded"
MODEL_ERROR = "model_error"


@dataclass(frozen=True)
class ToolError(Exception):
    """One tool call that did not produce a result."""

    code: str
    message: str
    retryable: bool = False

    def __str__(self):
        return f"{self.code}: {self.message}"

    def as_dict(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}