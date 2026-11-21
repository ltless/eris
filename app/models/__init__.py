"""Model transport, the thinking ladder, the declared capabilities, and the
server readout.

The runtime asks this package for a stream of deltas; nothing above it knows
the endpoint's URL shape, its NDJSON framing, or which of the two route
families it is on.
"""

from .caps import cached_capabilities, listed_capabilities
from .providers import (
    ModelError,
    healthy,
    list_models,
    merge_tool_calls,
    model_label,
    stream_completion,
    title_for,
)
from .stats import model_stats

__all__ = [
    "ModelError",
    "cached_capabilities", "healthy", "listed_capabilities",
    "list_models", "merge_tool_calls", "model_label", "model_stats",
    "stream_completion", "title_for",
]
