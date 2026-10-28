"""Backwards-compatible face for the permission rules.

The rules live in `app/security/permissions.py`; this module keeps the names
that were already imported from here.
"""

from ..security import (  # noqa: F401
    ALLOW,
    COMMAND,
    DESTRUCTIVE,
    EXTERNAL,
    LEVELS,
    NEEDS_APPROVAL,
    READ_ONLY,
    WRITE,
    Decision,
    approval_token,
    authorize,
    deny,
)
