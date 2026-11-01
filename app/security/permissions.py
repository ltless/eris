"""Tool permissions: what a tool is allowed to do.

Every tool declares a permission, and the runtime asks this module before
anything runs. Reading the world is allowed; changing it is not, because
nothing in this app can stop and ask the person whether they meant it.
"""

from dataclasses import dataclass

READ_ONLY = "read_only"
WRITE = "write"
COMMAND = "command"
EXTERNAL = "external"
DESTRUCTIVE = "destructive"

LEVELS = (READ_ONLY, WRITE, COMMAND, EXTERNAL, DESTRUCTIVE)

@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str = ""
    reason: str = ""

ALLOW = Decision(True)

def deny(code, reason):
    return Decision(False, code=code, reason=reason)

def authorize(tool, ctx=None, args=None):
    """Whether this tool may run.

    Returns a Decision. A denied call is reported to the model as a tool error
    and to the user as an event; it is never silently skipped.
    """
    level = tool.permission
    if level not in LEVELS:
        return deny("denied", f"tool {tool.name!r} declares an unknown permission {level!r}")

    if level in (READ_ONLY, EXTERNAL):
        return ALLOW

    # Everything below changes something, and nothing in this app can stop and
    # ask the person whether they meant it. The answer stays no rather than
    # becoming yes by default.
    return deny("denied", f"{tool.name} changes state, and nothing here can ask permission for it")