"""One envelope for everything the runtime does.

The API layer maps these onto the SSE frames the browser already speaks, so a
step means the same thing everywhere it is read.
"""

from dataclasses import dataclass, field

# Model calls.
MODEL_DELTA = "model.delta"
MODEL_THINKING = "model.thinking"

# What the model call cost, stated by the server on the frame that ends it.
MODEL_METRICS = "model.metrics"

# Tools.
TOOL_STARTED = "tool.started"
TOOL_COMPLETED = "tool.completed"
TOOL_FAILED = "tool.failed"

# Sources a tool or the retrieval pass put in front of the model.
SOURCE_FOUND = "source.found"

# How a run ended. There is no "completed" event: the stream's `[DONE]` is the
# end of a run that worked, and the failure is what the browser watches for.
TASK_FAILED = "task.failed"


@dataclass(frozen=True)
class Event:
    """One observable step. `data` is the whole of what it says; the order the
    events arrive in is the order the run did them."""

    type: str
    data: dict = field(default_factory=dict)


def emit(type, **data):
    return Event(type=type, data=data)