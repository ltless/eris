"""Security boundaries: what a tool may do.

Split from the tools that use it, because the rules are about the system rather
than about any one capability, and the registry needs the same answers.
"""

from .permissions import (
    ALLOW,
    COMMAND,
    DESTRUCTIVE,
    EXTERNAL,
    LEVELS,
    READ_ONLY,
    WRITE,
    Decision,
    authorize,
    deny,
)

__all__ = [
    "ALLOW", "COMMAND", "DESTRUCTIVE", "EXTERNAL", "LEVELS",
    "READ_ONLY", "WRITE", "Decision", "authorize",
    "deny",
]