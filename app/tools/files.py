"""Reading the files attached to a run.

A long attachment is named in the prompt rather than pasted into it. Every
round sends the whole conversation again, so text inlined once is paid for
again on each of them; a tool call reads the part the model asked for, once.
"""

from ..errors import TOOL_ERROR
from ..security import READ_ONLY
from .registry import TAGS, Tool, ToolResult, registry

DEFAULT_LENGTH = 8_000
NAME = "read_file"
TIMEOUT_S = 10.0

DESCRIPTION = (
    "Read text from a file attached to this message, by the name shown "
    "in the prompt. A long file comes back in parts; call again with "
    "`offset` set to the number the returned note gives you."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The file's name, as the prompt shows it.",
        },
        "offset": {
            "type": "integer",
            "description": "Character to start reading at. Default 0.",
        },
        "length": {
            "type": "integer",
            "description": f"Characters to return. Default {DEFAULT_LENGTH}.",
        },
    },
    "required": ["name"],
}


def _attached(ctx):
    """The files this run carries, as a tuple. One reader for both the tool and
    its `when`, so the two cannot disagree about what "attached" means."""
    return tuple(getattr(ctx, "files", ()) or ())


def _find(files, name):
    """The file the model named. A small model drops the extension or the case,
    so an exact match is tried first and a looser one after it."""
    for f in files:
        if f["name"] == name:
            return f
    low = name.lower()
    for f in files:
        if f["name"].lower() == low:
            return f
    for f in files:
        if f["name"].lower().startswith(low) or low.endswith(f["name"].lower()):
            return f
    return None


def read(args, ctx):
    """Answer the read_file tool, or say which failure stopped it."""
    files = _attached(ctx)
    if not files:
        return ToolResult.failed(
            TOOL_ERROR, "No file is attached to this message.",
            reason="no file is attached",
        )
    name = str(args.get("name", "") or "").strip()
    if not name:
        names = ", ".join(f["name"] for f in files)
        return ToolResult.failed(
            TOOL_ERROR, f"No file named. Attached: {names}.",
            reason="no file was named",
        )
    found = _find(files, name)
    if found is None:
        names = ", ".join(f["name"] for f in files)
        return ToolResult.failed(
            TOOL_ERROR, f"No file named {name!r}. Attached: {names}.",
            reason="no such attachment",
        )

    text = found.get("text") or ""
    try:
        offset = max(int(args.get("offset") or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        length = int(args.get("length") or DEFAULT_LENGTH)
    except (TypeError, ValueError):
        length = DEFAULT_LENGTH
    # No upper clamp on the slice: attached text is already capped where it is
    # stored, so a huge `length` can only return what is left of the file.
    length = max(length, 1)
    if offset >= len(text):
        return ToolResult.failed(
            TOOL_ERROR,
            f"{name} holds {len(text)} characters; {offset} is past its end.",
            reason="offset past the end",
        )

    part = text[offset : offset + length]
    end = offset + len(part)
    body = f"File: {name} (characters {offset}-{end} of {len(text)})\n{part}"
    if end < len(text):
        body += (f"\n[... {len(text) - end} more characters; call read_file with "
                 f"name={name!r} and offset={end}]")
    return ToolResult.ok(body)


def _has_files(ctx):
    return bool(_attached(ctx))


def register():
    return registry.add(Tool(
        name=NAME,
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=read,
        permission=READ_ONLY,
        timeout=TIMEOUT_S,
        tags=TAGS,
        when=_has_files,
    ))
