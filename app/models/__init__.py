"""Model selection, transport, and capability reporting.

The runtime asks this package for a model and a stream of deltas; nothing above
it knows the endpoint's URL shape, its NDJSON framing, or which of the two
route families it is on.
"""

from .capabilities import Capabilities, ModelInfo, for_configured_backend
from .providers import (
    FALLBACK_LADDER,
    THINK_LEVELS,
    ModelError,
    base_url,
    cached_ladder,
    chat_url,
    effort_label,
    healthy,
    list_models,
    merge_tool_calls,
    model_label,
    normalize_call,
    parse_chunk,
    parse_metrics,
    rung_index,
    stream_completion,
    think_ladder,
    think_value,
    to_native,
)
from .router import CHAT, RESEARCH, VERIFICATION, NoModelAvailable, Router, build
from .stats import model_stats

__all__ = [
    "CHAT", "RESEARCH", "VERIFICATION",
    "FALLBACK_LADDER", "THINK_LEVELS",
    "Capabilities", "ModelError", "ModelInfo", "NoModelAvailable", "Router",
    "base_url", "build", "cached_ladder", "chat_url", "effort_label",
    "for_configured_backend", "healthy",
    "list_models", "merge_tool_calls", "model_label", "normalize_call",
    "model_stats", "parse_chunk", "parse_metrics", "rung_index",
    "stream_completion",
    "think_ladder", "think_value", "to_native",
]
