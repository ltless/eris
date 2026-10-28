"""One envelope for everything the runtime does.

The API layer maps these onto the SSE frames the browser already speaks, so a
step means the same thing everywhere it is read.

Payloads are scrubbed on the way out: a key that looks like a credential is
replaced, and long strings are capped. Events reach the browser, and should
carry neither a secret nor an unbounded tool dump.
"""

import time
from dataclasses import dataclass, field

# Model calls.
MODEL_STARTED = "model.started"
MODEL_DELTA = "model.delta"
MODEL_THINKING = "model.thinking"
MODEL_COMPLETED = "model.completed"

# What the model call cost, stated by the server on the frame that ends it.
MODEL_METRICS = "model.metrics"

# Tools.
TOOL_STARTED = "tool.started"
TOOL_COMPLETED = "tool.completed"
TOOL_FAILED = "tool.failed"

# Sources a tool or the retrieval pass put in front of the model.
SOURCE_FOUND = "source.found"

# Approvals.
APPROVAL_REQUIRED = "approval.required"

# How a run ended.
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"
TASK_CANCELLED = "task.cancelled"

MAX_TEXT = 20_000

SECRET_KEYS = frozenset(
    {
        "api_key", "apikey", "authorization", "cookie", "password", "passwd",
        "secret", "session", "token", "access_token", "refresh_token", "pw",
    }
)


def scrub(value, key=None, depth=0):
    """Drop credentials and cap long strings, recursively.

    A dict whose key names a credential is replaced wholesale rather than
    filtered: the shape stays, the value does not.
    """
    if key and key.lower() in SECRET_KEYS:
        return "[redacted]"
    if depth > 12:
        return "[deep]"
    if isinstance(value, dict):
        return {k: scrub(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, None, depth + 1) for v in value]
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"... [{len(value) - MAX_TEXT} chars omitted]"
    return value


@dataclass(frozen=True)
class Event:
    """One observable step, numbered within its run."""

    type: str
    seq: int
    ts: float
    data: dict = field(default_factory=dict)

    def as_dict(self):
        return {"type": self.type, "seq": self.seq, "ts": self.ts, "data": scrub(self.data)}


class Emitter:
    """Numbering for one run. `seq` is what a reconnecting client replays from."""

    def __init__(self, start=0):
        self.seq = start

    def emit(self, type, **data):
        self.seq += 1
        return Event(type=type, seq=self.seq, ts=time.time(), data=data)