"""Where a tool that writes is allowed to write.

`read_file` answers about text the run was handed, so it can only ever reach
what the reader already chose to attach. A writing tool reaches the disk
instead, and a path the model composed is the one input in this app that no
person typed. The boundary therefore has to be a real one, checked at the point
of use, rather than a sentence in a description asking the model to behave.

The rule is one root, named once in the environment. Every path a call carries
is resolved against it, and anything that lands outside is refused before a file
is opened. Symlinks are refused rather than followed: a link inside the root can
point anywhere, and `resolve()` alone would make the outside look like the
inside.

No root is set by default. Writing is the one capability a reader cannot undo by
re-reading, so it stays off until somebody names a directory for it.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Reserved device names on Windows. A file may not be called these, with or
# without an extension: `NUL` and `CON` are devices, not names.
_DEVICES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# Characters Windows refuses in a filename at all, plus the two the app cannot
# carry in a path it hands to the filesystem or to a shell.
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

# One path's worth of room. Long enough for nested folders, short enough that
# concatenating it onto the root cannot approach the platform's limit.
MAX_PATH_CHARS = 200


@dataclass(frozen=True)
class Access:
    root: Path
    writable: bool = False


def root():
    """The directory writes are confined to, or `None` when none is named.

    Read from the environment on each call rather than cached at import, so a
    test can point it at a temporary directory without reloading this module.
    """
    raw = (os.getenv("WORKSPACE_DIR") or "").strip()
    if not raw:
        return None
    try:
        path = Path(raw).expanduser().resolve(strict=True)
        return path if path.is_dir() else None
    except (OSError, RuntimeError):
        return None


def for_user(user_id):
    if os.getenv("WORKSPACE_USER_ID", "").strip() != str(user_id):
        return None
    path = root()
    if path is None:
        return None
    return Access(path, writable=os.getenv("WORKSPACE_ALLOW_WRITE") == "1")


def resolve(name, base=None):
    """`(path, error)`. `path` is a file inside the root; `error` is why not.

    The name is treated as relative to the root and nothing else. An absolute
    path, a drive letter, `..`, or a reserved name is refused by looking at what
    was asked for; a symlink or a path that lands outside after normalizing is
    refused by looking at what it would have become. Both checks are needed:
    the first is what the model wrote, the second is what the filesystem would
    have done with it.
    """
    base = base or root()
    if base is None:
        return None, "no workspace directory is configured"

    raw = str(name or "").strip()
    if not raw:
        return None, "no path given"
    if len(raw) > MAX_PATH_CHARS:
        return None, f"path is longer than {MAX_PATH_CHARS} characters"

    # A quoted path is the model repeating what it saw, not asking for a
    # literal quotation mark in the filename.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
        if not raw:
            return None, "no path given"

    # Windows reads a colon as an alternate data stream and a leading slash as
    # another drive, so both are refused rather than normalized away. This runs
    # before the character check, which also rejects a colon: the reason a path
    # was refused should name the escape, not the punctuation.
    if Path(raw).is_absolute() or raw.startswith(("/", "\\")) or ":" in raw:
        return None, "path must be relative to the workspace"
    if _BAD_CHARS.search(raw):
        return None, "path holds a character a filename cannot have"
    parts = [p for p in re.split(r"[\\/]+", raw) if p not in ("", ".")]
    if not parts:
        return None, "no path given"
    if any(p == ".." for p in parts):
        return None, "path may not climb out of the workspace"
    for part in parts:
        if part.rstrip(". ") != part:
            return None, "a path part may not end in a dot or a space"
        stem = part.split(".")[0].upper()
        if stem in _DEVICES:
            return None, f"{part!r} names a device, not a file"

    full = base.joinpath(*parts)
    # Resolve without requiring existence: a file about to be created has no
    # final component yet, and the point here is where it would land.
    try:
        real = full.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, "path could not be resolved"
    if real != base and base not in real.parents:
        return None, "path lands outside the workspace"

    # Every existing component is walked by hand. A link anywhere along the way
    # leads somewhere the resolved check above cannot see, because resolving a
    # link and then comparing prefixes is exactly how the escape would succeed.
    walk = base
    for part in parts:
        walk = walk / part
        try:
            if walk.is_symlink() or (hasattr(walk, "is_junction") and walk.is_junction()):
                return None, "workspace path may not be a link"
        except OSError:
            return None, "path could not be inspected"

    return real, None
