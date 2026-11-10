"""Security boundaries: what a tool may do.

Split from the tools that use it, because the rules are about the system rather
than about any one capability, and the registry needs the same answers.

Only the names a caller outside this package reads are re-exported. The rest of
the levels live in `permissions` and are reached from there.
"""

from .permissions import EXTERNAL, READ_ONLY, authorize

__all__ = ["EXTERNAL", "READ_ONLY", "authorize"]
