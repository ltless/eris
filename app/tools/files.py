"""Reading the files attached to a run.

A long attachment is named in the prompt rather than pasted into it. Every
round sends the whole conversation again, so text inlined once is paid for
again on each of them; a tool call reads the part the model asked for, once.
"""

from . import as_args

DEFAULT_LENGTH = 8_000
MAX_LENGTH = 40_000

TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read text from a file attached to this message, by the name shown "
            "in the prompt. A long file comes back in parts; call again with "
            "`offset` set to the number the returned note gives you."
        ),
        "parameters": {
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
        },
    },
}


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
    """`(ok, text, reason)` for one attached file. `reason` is empty on
    success, and names the failure apart from the text the model reads."""
    args = as_args(args)
    files = tuple(getattr(ctx, "files", ()) or ())
    if not files:
        return False, "No file is attached to this message.", "no file is attached"
    name = str(args.get("name", "") or "").strip()
    if not name:
        names = ", ".join(f["name"] for f in files)
        return False, f"No file named. Attached: {names}.", "no file was named"
    found = _find(files, name)
    if found is None:
        names = ", ".join(f["name"] for f in files)
        return False, f"No file named {name!r}. Attached: {names}.", "no such attachment"

    text = found.get("text") or ""
    try:
        offset = max(int(args.get("offset") or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        length = int(args.get("length") or DEFAULT_LENGTH)
    except (TypeError, ValueError):
        length = DEFAULT_LENGTH
    length = min(max(length, 1), MAX_LENGTH)
    if offset >= len(text):
        return False, f"{name} holds {len(text)} characters; {offset} is past its end.", "offset past the end"

    part = text[offset : offset + length]
    end = offset + len(part)
    body = f"File: {name} (characters {offset}-{end} of {len(text)})\n{part}"
    if end < len(text):
        body += f"\n[... {len(text) - end} more characters; call read_file with name={name!r} and offset={end}]"
    return True, body, ""


async def run(args, ctx):
    """Handler for the read_file tool. Returns text for the model, never raises."""
    return read(args, ctx)[1]