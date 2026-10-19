"""The Library's document index in sqlite.

Chroma holds the vectors but cannot answer "what is indexed": its chunk
metadata has no date, no size, and no id of its own, so two uploads of one
filename would merge into a single row. This table is the record of each
upload, and every chunk in Chroma carries the same id in `doc_id`, which is
what keeps the two in step.
"""

import os
import sqlite3
import time
from pathlib import Path

_db = None


def db_path():
    return Path(os.getenv("LIBRARY_DB", "library.db"))


def db():
    global _db
    if _db is None:
        fresh = not db_path().exists()
        _db = sqlite3.connect(db_path(), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        if fresh:
            _db.executescript(
                "CREATE TABLE documents("
                "id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                "name TEXT NOT NULL, bytes INTEGER NOT NULL, "
                "chunks INTEGER NOT NULL, created INTEGER NOT NULL);"
                "CREATE INDEX documents_user ON documents(user_id);"
            )
    return _db


def add(user_id, doc_id, name, size, chunks):
    now = int(time.time())
    db().execute(
        "INSERT INTO documents(id,user_id,name,bytes,chunks,created) VALUES(?,?,?,?,?,?)",
        (doc_id, user_id, name, size, chunks, now),
    )
    db().commit()
    return {"id": doc_id, "name": name, "bytes": size, "chunks": chunks, "created": now}


def list_docs(user_id):
    rows = db().execute(
        "SELECT id,name,bytes,chunks,created FROM documents WHERE user_id=? "
        "ORDER BY created DESC, rowid DESC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def find_by_name(user_id, name):
    """Every row this user has under one name.

    More than one is possible: uploads used to append, so the same filename
    could be indexed several times. A re-upload retires all of them.
    """
    rows = db().execute(
        "SELECT id,name,bytes,chunks,created FROM documents "
        "WHERE user_id=? AND name=? ORDER BY created DESC, rowid DESC",
        (user_id, name),
    ).fetchall()
    return [dict(r) for r in rows]


def get(user_id, doc_id):
    row = db().execute(
        "SELECT id,name,bytes,chunks,created FROM documents WHERE id=? AND user_id=?",
        (doc_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def delete(user_id, doc_id):
    db().execute("DELETE FROM documents WHERE id=? AND user_id=?", (doc_id, user_id))
    db().commit()
