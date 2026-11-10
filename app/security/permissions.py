"""Tool permissions: what a tool is allowed to do.

Every tool declares a permission, and the runtime asks this module before
anything runs. Reading the world is allowed; changing it is not, because
nothing in this app can stop and ask the person whether they meant it.
"""

READ_ONLY = "read_only"
EXTERNAL = "external"
WRITE = "write"
COMMAND = "command"
DESTRUCTIVE = "destructive"

LEVELS = (READ_ONLY, WRITE, COMMAND, EXTERNAL, DESTRUCTIVE)

# What the runtime allows. `authorize` answers from this one set, so "may this
# run" has a single answer rather than one per call site.
ALLOWED = (READ_ONLY, EXTERNAL)


def authorize(tool):
    """`None` when the tool may run, else `(code, reason)` for the refusal.

    A denied call is reported to the model as a tool error and to the user as
    an event; it is never silently skipped. The answer reads the declared
    permission and nothing else, so no caller has to supply context it will
    not be asked about.
    """
    level = tool.permission
    if level not in LEVELS:
        return "denied", f"tool {tool.name!r} declares an unknown permission {level!r}"
    if level in ALLOWED:
        return None
    # Everything else changes something. Nothing here can stop and ask the
    # person whether they meant it, so the answer stays no rather than
    # becoming yes by default.
    return "denied", f"{tool.name} changes state, and nothing here can ask permission for it"
