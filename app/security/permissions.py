"""Tool permissions: what a tool is allowed to do, and who said so.

Every tool declares a permission. This turns a permission, call arguments, and
user approvals into a yes/no decision. The runtime asks before anything runs.

An approval names one operation — this tool, these arguments — and is spent on
it. There is no way to say "yes to everything from now on", which prevents a
granted read from becoming a write.
"""

import hashlib
import json
from dataclasses import dataclass

READ_ONLY = "read_only"
WRITE = "write"
COMMAND = "command"
EXTERNAL = "external"
DESTRUCTIVE = "destructive"

LEVELS = (READ_ONLY, WRITE, COMMAND, EXTERNAL, DESTRUCTIVE)

# Reads don't need approval, even network ones: the operator asked a question.
NEEDS_APPROVAL = frozenset({WRITE, COMMAND, DESTRUCTIVE})


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str = ""
    reason: str = ""


ALLOW = Decision(True)


def deny(code, reason):
    return Decision(False, code=code, reason=reason)


def approval_token(tool_name, args):
    """A stable name for one specific operation.

    The token covers the tool and its arguments, so approving a patch to one
    file does not approve a patch to another, and a second call with different
    arguments needs its own approval.
    """
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{digest}"


def authorize(tool, ctx, args=None):
    """Whether this tool may run for this context, with these arguments.

    Returns a Decision. A denied call is reported to the model as a tool error
    and to the user as an event; it is never silently skipped.
    """
    level = tool.permission
    if level not in LEVELS:
        return deny("denied", f"tool {tool.name!r} declares an unknown permission {level!r}")

    if level in (READ_ONLY, EXTERNAL):
        return ALLOW

    # Everything below changes something, so it needs an approval.
    # An approval is bound to one operation: this tool, these arguments.
    if approval_token(tool.name, args or {}) in (ctx.approved or set()):
        return ALLOW

    return deny(
        "approval_required",
        f"{tool.name} changes state and has not been approved for this call",
    )