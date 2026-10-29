"""Opening a sqlite file: the part every store does the same way.

Each store keeps its own `_db` rather than sharing a connection, because the
tests rebind the one they care about.
"""

import os
import sqlite3
from pathlib import Path


def path_from_env(var, default):
    return Path(os.getenv(var, default))


def open_db(existing, path, schema, migrate=None):
    """Return `existing` if it is open, else open `path` and prepare the schema.

    `schema` runs only on a file that did not exist yet; an existing file goes
    to `migrate`, so a column added later lands without a rebuild.
    """
    if existing is not None:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if fresh:
        conn.executescript(schema)
    elif migrate is not None:
        migrate(conn)
    return conn
