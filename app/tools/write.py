"""Writing a file into the run's workspace.

The run needs workspace write access. Each path is checked against that
workspace, and replacing an existing file requires `overwrite`.
"""

import json

from ..errors import TOOL_ERROR
from ..security import WRITE
from ..security.workspace import resolve
from .registry import TAGS, Tool, ToolResult, registry

NAME = "write_file"
TIMEOUT_S = 15.0

# What one call may leave on disk. Large enough for a page of text or a data
# file, small enough that a runaway loop asking for a rewrite cannot fill the
# volume before someone notices.
MAX_BYTES = 400_000


def _content(args):
    """The text to write. A model that sent a structure rather than a string is
    writing JSON, so the mapping is serialized the way the model meant it."""
    value = args.get("content")
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)

def write(args, ctx):
    """Answer the write_file tool, or say which refusal stopped it."""
    if ctx.workspace is None or not ctx.workspace.writable:
        return ToolResult.failed("denied", "Workspace write access is required.")
    raw = str(args.get("path", "") or "").strip()
    if not raw:
        return ToolResult.failed(
            TOOL_ERROR, "No path given.", reason="no path was given",
        )

    content = _content(args)
    if content is None:
        return ToolResult.failed(
            TOOL_ERROR, "No content given.", reason="no content was given",
        )
    blob = content.encode("utf-8")
    if len(blob) > MAX_BYTES:
        return ToolResult.failed(
            TOOL_ERROR,
            f"That content is {len(blob)} bytes; one call may write {MAX_BYTES}.",
            reason="the content was too large",
        )

    target, why = resolve(raw, ctx.workspace.root)
    if target is None:
        return ToolResult.failed(TOOL_ERROR, f"{raw!r} was refused: {why}.", reason=why)

    existed = target.exists()
    if existed and not target.is_file():
        return ToolResult.failed(
            TOOL_ERROR, f"{raw!r} is a directory, not a file.",
            reason="that path is a directory",
        )
    if existed and args.get("overwrite") is not True:
        return ToolResult.failed(
            TOOL_ERROR,
            f"{raw!r} already exists. Call again with `overwrite` set to true "
            "to replace it.",
            reason="the file already exists",
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    except OSError as e:
        return ToolResult.failed(
            TOOL_ERROR, f"{raw!r} could not be written ({e.strerror or e}).",
            reason="the file could not be written",
        )

    verb = "Replaced" if existed else "Wrote"
    card = {
        "path": raw,
        "bytes": len(blob),
        "replaced": existed,
    }
    return ToolResult.ok(
        f"{verb} {raw} ({len(blob)} bytes).", card=card,
    )


def register():
    return registry.add(Tool(
        name=NAME,
        description=(
            "Write text to a file in the workspace. `path` is relative to the "
            "workspace root: an absolute path, a `..`, or a link is refused. "
            "Missing folders in the path are created. Writing over an existing "
            "file needs `overwrite` set to true. Report what you wrote in your "
            "reply; do not paste the file back."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Where to write, relative to the workspace root.",
                },
                "content": {
                    "type": "string",
                    "description": "The whole text of the file, not a fragment.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Required to replace a file that already exists.",
                },
            },
            "required": ["path", "content"],
        },
        handler=write,
        permission=WRITE,
        timeout=TIMEOUT_S,
        tags=TAGS,
        carded=True,
    ))
