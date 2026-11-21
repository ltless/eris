"""Reading the workspace: one file's text, or what a folder holds.

A long attachment is named in the prompt rather than pasted into it. Every
round sends the whole conversation again, so text inlined once is paid for
again on each of them; a tool call reads the part the model asked for, once.

Both sources of text page the same way; only where the text came from differs.
A workspace path is held to the same boundary a write is: it is resolved by
`app/security/workspace.py` before anything is opened. `list_workspace` reads
that boundary too, so a run can see what it already wrote before writing over
it, and both tools go dark together when no root is named.
"""

from ..errors import TOOL_ERROR
from ..security import READ_ONLY
from ..security.workspace import resolve
from .registry import TAGS, Tool, ToolResult, registry

DEFAULT_LENGTH = 8_000
# What one read may load out of the workspace. A file this app wrote is capped
# far below it; the bound is for a workspace pointed at a directory holding
# something larger.
MAX_READ_BYTES = 2_000_000
NAME = "read_file"
TIMEOUT_S = 10.0
LIST_NAME = "list_workspace"
# A listing is read to find a name, not to survey a volume, so one call names a
# bounded number of entries and points at a subfolder for the rest.
MAX_ENTRIES = 200

DESCRIPTION = (
    "Read text from a file: one attached to this message, by the name the "
    "prompt shows, or one in the workspace, by its path. A long file comes "
    "back in parts; call again with `offset` set to the number the returned "
    "note gives you."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The file's name as the prompt shows it, or its path "
                           "inside the workspace.",
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

LIST_DESCRIPTION = (
    "List what a folder in the workspace holds, by path. Call it before "
    "writing over a name you are not sure about, or to find a file a previous "
    "turn wrote. Omit `path` for the workspace root."
)
LIST_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Folder to list, relative to the workspace root. "
                           "Omit it to list the root.",
        },
    },
    "required": [],
}


def _attached(ctx):
    """The files this run carries, as a tuple. One reader for both the tool and
    its `when`, so the two cannot disagree about what "attached" means."""
    return tuple(getattr(ctx, "files", ()) or ())


def _names(files):
    return ", ".join(f["name"] for f in files)


def _find(files, name):
    """The attached file the model named. A small model drops the extension or
    the case, so an exact match is tried first and a looser one after it. A name
    holding a separator is a workspace path, not a filename, so the loose pass
    is skipped for it: `notes/a.md` should not match an attachment `a.md`."""
    for f in files:
        if f["name"] == name:
            return f
    if "/" in name or "\\" in name:
        return None
    low = name.lower()
    for f in files:
        if f["name"].lower() == low:
            return f
    for f in files:
        if f["name"].lower().startswith(low) or low.endswith(f["name"].lower()):
            return f
    return None


def _slice(name, text, args):
    """The part of the file the call asked for, and the note saying what is
    left. Every source of text ends here, so one call reads the same way
    whether the file was attached or found in the workspace."""
    try:
        offset = max(int(args.get("offset") or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        length = int(args.get("length") or DEFAULT_LENGTH)
    except (TypeError, ValueError):
        length = DEFAULT_LENGTH
    # No upper clamp on the slice: what a file may hold is capped where it is
    # stored or read, so a huge `length` can only return what is left of it.
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


def _from_workspace(name, args, base):
    """A file in the workspace, reached through the same guard a write is."""
    path, why = resolve(name, base)
    if path is None:
        return ToolResult.failed(TOOL_ERROR, f"{name!r} was refused: {why}.", reason=why)
    if path.is_dir():
        return ToolResult.failed(
            TOOL_ERROR, f"{name!r} is a directory, not a file.",
            reason="that path is a directory",
        )
    # Stat before reading, so a workspace holding something huge is refused
    # rather than loaded into memory first.
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return ToolResult.failed(
            TOOL_ERROR, f"No file at {name!r} in the workspace.",
            reason="no such workspace file",
        )
    except OSError as e:
        return ToolResult.failed(
            TOOL_ERROR, f"{name!r} could not be read ({e.strerror or e}).",
            reason="the file could not be read",
        )
    if size > MAX_READ_BYTES:
        return ToolResult.failed(
            TOOL_ERROR,
            f"{name} is {size} bytes; read_file reads up to {MAX_READ_BYTES}.",
            reason="the file is too large to read",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ToolResult.failed(
            TOOL_ERROR, f"No file at {name!r} in the workspace.",
            reason="no such workspace file",
        )
    except UnicodeDecodeError:
        return ToolResult.failed(
            TOOL_ERROR, f"{name!r} is not text.", reason="the file is not text",
        )
    except OSError as e:
        return ToolResult.failed(
            TOOL_ERROR, f"{name!r} could not be read ({e.strerror or e}).",
            reason="the file could not be read",
        )
    return _slice(name, text, args)


def read(args, ctx):
    """Answer the read_file tool, or say which failure stopped it."""
    files = _attached(ctx)
    workspace = ctx.workspace
    if not files and workspace is None:
        return ToolResult.failed(
            TOOL_ERROR, "No file is attached to this message.",
            reason="no file is attached",
        )
    name = str(args.get("name", "") or "").strip()
    if not name:
        if files:
            return ToolResult.failed(
                TOOL_ERROR, f"No file named. Attached: {_names(files)}.",
                reason="no file was named",
            )
        return ToolResult.failed(TOOL_ERROR, "No path given.", reason="no path given")

    found = _find(files, name) if files else None
    if found is not None:
        return _slice(name, found.get("text") or "", args)
    if workspace is None:
        return ToolResult.failed(
            TOOL_ERROR, f"No file named {name!r}. Attached: {_names(files)}.",
            reason="no such attachment",
        )
    return _from_workspace(name, args, workspace.root)


def _offered(ctx):
    """Offered when there is something to read: an attachment, or a workspace
    whose paths the model may name."""
    return bool(_attached(ctx)) or ctx.workspace is not None


def _in_workspace(ctx):
    """`list_workspace` reads folders, so an attachment is not a substitute for
    a root: without one there is nothing for it to list."""
    return ctx.workspace is not None


def _size(n):
    """A file size a reader can hold in their head, rounded to one decimal."""
    value = float(n)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _row(entry):
    """One line of a listing. A link is named as one rather than reported as
    the folder it points at, because the guard refuses to follow it and a
    listing that said otherwise would be describing somewhere the next call
    cannot go."""
    name = entry.name
    if entry.is_symlink():
        return f"{name} (link)"
    if entry.is_dir():
        return f"{name}/"
    try:
        return f"{name} ({_size(entry.stat().st_size)})"
    except OSError:
        return name


def listing(args, ctx):
    """What a workspace folder holds, or why it could not be listed."""
    if not _in_workspace(ctx):
        return ToolResult.failed(
            TOOL_ERROR, "No workspace is configured.", reason="no workspace directory",
        )
    raw = str(args.get("path", "") or "").strip()
    base = ctx.workspace.root
    target = base
    if raw:
        target, why = resolve(raw, base)
        if target is None:
            return ToolResult.failed(TOOL_ERROR, f"{raw!r} was refused: {why}.", reason=why)
    if not target.is_dir():
        return ToolResult.failed(
            TOOL_ERROR, f"{raw!r} is not a folder." if raw else
            "The workspace root is not a folder.",
            reason="that path is not a folder",
        )
    try:
        # Folders first, then files, each alphabetically: a listing is read to
        # find a name, so the order should not be the filesystem's.
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e:
        return ToolResult.failed(
            TOOL_ERROR, f"The workspace could not be listed ({e.strerror or e}).",
            reason="the workspace could not be listed",
        )

    where = f"workspace/{raw.strip('/')}" if raw else "the workspace"
    if not entries:
        return ToolResult.ok(f"{where} is empty.")
    shown = entries[:MAX_ENTRIES]
    count = f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
    body = f"{where} holds {count}:\n" + "\n".join(_row(e) for e in shown)
    if len(entries) > MAX_ENTRIES:
        body += f"\n[... {len(entries) - MAX_ENTRIES} more; ask for a subfolder]"
    return ToolResult.ok(body)


def register():
    registry.add(Tool(
        name=NAME,
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=read,
        permission=READ_ONLY,
        timeout=TIMEOUT_S,
        tags=TAGS,
        when=_offered,
    ))
    return registry.add(Tool(
        name=LIST_NAME,
        description=LIST_DESCRIPTION,
        parameters=LIST_PARAMETERS,
        handler=listing,
        permission=READ_ONLY,
        timeout=TIMEOUT_S,
        tags=TAGS,
        when=_in_workspace,
    ))
