"""Model transport, the thinking ladder, the declared capabilities, and the
server readout.

The runtime asks this package for a stream of deltas; nothing above it knows
the endpoint's URL shape, its NDJSON framing, or which of the two route
families it is on.
"""

from .caps import cached_capabilities, capabilities, listed_capabilities
from .endpoint import base_url, chat_url
from .ladder import (
    FALLBACK_LADDER,
    THINK_LEVELS,
    cached_ladder,
    effort_label,
    rung_index,
    think_ladder,
    think_value,
)
from .providers import (
    ModelError,
    healthy,
    list_models,
    merge_tool_calls,
    model_label,
    normalize_call,
    parse_chunk,
    parse_metrics,
    stream_completion,
    to_native,
)
from .stats import model_stats

__all__ = [
    "FALLBACK_LADDER", "THINK_LEVELS",
    "ModelError",
    "base_url", "cached_capabilities", "cached_ladder", "capabilities", "chat_url",
    "effort_label", "healthy", "listed_capabilities",
    "list_models", "merge_tool_calls", "model_label", "normalize_call",
    "model_stats", "parse_chunk", "parse_metrics", "rung_index",
    "stream_completion",
    "think_ladder", "think_value", "to_native",
]
