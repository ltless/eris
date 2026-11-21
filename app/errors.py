"""Failures the app can name.

A tool failure reaches the model as a tool message and the browser as an event,
so both need to be told what went wrong. An exception's class name is not that
answer, so each one is normalized to a code, a message, and whether retrying
the same call could plausibly work.

The codes sit above both the tools that raise them and the runtime that reports
them, so neither layer has to reach into the other for a name.
"""

from dataclasses import dataclass

# Codes the runtime emits. The frontend switches on these.
UNKNOWN_TOOL = "unknown_tool"
TOOL_NOT_OFFERED = "tool_not_offered"
INVALID_ARGUMENTS = "invalid_arguments"
TOOL_ERROR = "tool_error"
TIMEOUT = "timeout"
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
