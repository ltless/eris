"""Long-lived API tokens, in sqlite.

A session cookie belongs to one browser and dies with it. A token is for
scripts, editors, or agents speaking MCP. Only the hash is stored; the
plaintext exists once in the response that created it.

Tokens are scoped to read: they may call the API and MCP server, but are
refused at routes that change the account itself.
"""

import hashlib
import secrets
import time

from .encoding import new_id
from .store import open_db, path_from_env

MAX_TOKENS = 20
MAX_NAME = 60

PREFIX = "eris_"

SCHEMA = (
    "CREATE TABLE tokens("
    "id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
    "name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, "
    "prefix TEXT NOT NULL, created INTEGER NOT NULL, "
    "used INTEGER NOT NULL DEFAULT 0);"
    "CREATE INDEX tokens_user ON tokens(user_id);"
)

_db = None


def db_path():
    return path_from_env("TOKENS_DB", "database/tokens.db")


def db():
    global _db
    _db = open_db(_db, db_path(), SCHEMA)
    return _db


def clean_name(text):
    return " ".join(str(text or "").split())[:MAX_NAME]


def _hash(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _row(r):
    return {
        "id": r["id"], "name": r["name"], "prefix": r["prefix"],
        "created": r["created"], "used": r["used"] or 0,
    }


def count(user_id):
    row = db().execute("SELECT COUNT(*) AS n FROM tokens WHERE user_id=?",
                       (user_id,)).fetchone()
    return row["n"]


def create(user_id, name):
    """Mint a token. The plaintext is in the return value and nowhere else."""
    label = clean_name(name) or "Untitled token"
    if count(user_id) >= MAX_TOKENS:
        return None
    value = PREFIX + secrets.token_urlsafe(32)
    token_id = new_id()
    now = int(time.time())
    db().execute(
        "INSERT INTO tokens(id,user_id,name,hash,prefix,created,used) VALUES(?,?,?,?,?,?,0)",
        (token_id, user_id, label, _hash(value), value[:len(PREFIX) + 6], now),
    )
    db().commit()
    return {**get(user_id, token_id), "token": value}


def list_all(user_id):
    rows = db().execute(
        "SELECT * FROM tokens WHERE user_id=? ORDER BY created DESC, rowid DESC",
        (user_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def get(user_id, token_id):
    row = db().execute("SELECT * FROM tokens WHERE id=? AND user_id=?",
                       (token_id, user_id)).fetchone()
    return _row(row) if row else None


def revoke(user_id, token_id):
    cur = db().execute("DELETE FROM tokens WHERE id=? AND user_id=?", (token_id, user_id))
    db().commit()
    return cur.rowcount > 0


def resolve(value):
    """The account a presented token belongs to, or None.

    The lookup is by hash, so the plaintext is never compared and never stored.
    `used` is stamped on every successful resolve: an unused token in the list
    is the one worth revoking.
    """
    value = str(value or "").strip()
    if not value.startswith(PREFIX):
        return None
    row = db().execute("SELECT id,user_id FROM tokens WHERE hash=?", (_hash(value),)).fetchone()
    if not row:
        return None
    db().execute("UPDATE tokens SET used=? WHERE id=?", (int(time.time()), row["id"]))
    db().commit()
    return row["user_id"]
