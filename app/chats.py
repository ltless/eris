"""Chat persistence in sqlite. Turns are replayed, never searched, so no vector store here."""

import json
import os
import sqlite3
import time
from pathlib import Path

MAX_THINKING = 8000
MAX_SOURCES = 8

_db = None


def db_path():
    return Path(os.getenv("CHAT_DB", "chats.db"))


def db():
    global _db
    if _db is None:
        fresh = not db_path().exists()
        _db = sqlite3.connect(db_path(), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        if fresh:
            _db.executescript(
                "CREATE TABLE chats("
                "id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                "title TEXT NOT NULL DEFAULT 'New chat', "
                "pinned INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL);"
                "CREATE TABLE turns("
                "chat_id TEXT NOT NULL, role TEXT NOT NULL, "
                "content TEXT NOT NULL, thinking TEXT, sources TEXT, "
                "web TEXT, ts INTEGER NOT NULL);"
                "CREATE INDEX turns_chat ON turns(chat_id);"
            )
        else:
            cols = {r["name"] for r in _db.execute("PRAGMA table_info(turns)")}
            if "web" not in cols:
                _db.execute("ALTER TABLE turns ADD COLUMN web TEXT")
    return _db


def save_turn(user_id, chat_id, role, text, thinking=None, sources=None, web=None):
    if not chat_id or not text.strip():
        return
    # chat_id comes from the client, so a turn may only land in a chat the
    # caller owns. A missing row means this is the first turn of a new chat.
    owner = db().execute("SELECT user_id FROM chats WHERE id=?", (chat_id,)).fetchone()
    if owner and owner["user_id"] != user_id:
        return
    now = int(time.time() * 1000)
    meta = json.dumps(sources[:MAX_SOURCES]) if sources else None
    if isinstance(thinking, list):
        # Truncating the serialized JSON would leave it unparseable, so trim by
        # whole rounds and capped text until the encoding itself fits.
        kept = [part for part in thinking if part.strip()]
        while kept and len(json.dumps(kept)) > MAX_THINKING:
            over = len(json.dumps(kept)) - MAX_THINKING
            kept[-1] = kept[-1][: max(len(kept[-1]) - over, 0)]
            if not kept[-1]:
                kept.pop()
        thinking = json.dumps(kept) if kept else None
    elif thinking:
        thinking = thinking[:MAX_THINKING]
    webj = json.dumps(web) if web else None
    db().execute(
        "INSERT INTO turns(chat_id,role,content,thinking,sources,web,ts) VALUES(?,?,?,?,?,?,?)",
        (chat_id, role, text, thinking or None, meta, webj, now),
    )
    db().execute(
        "INSERT INTO chats(id,user_id,ts) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts",
        (chat_id, user_id, now),
    )
    db().commit()


def touch_title(chat_id, text):
    """Set title from first user message."""
    row = db().execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
    if row and row["title"] == "New chat":
        db().execute("UPDATE chats SET title=? WHERE id=?", (text.strip()[:40] or "New chat", chat_id))
        db().commit()


def list_chats(user_id, limit=50):
    rows = db().execute(
        "SELECT id,title,pinned FROM chats WHERE user_id=? "
        "ORDER BY pinned DESC, ts DESC, rowid DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [{"id": r["id"], "title": r["title"], "pinned": bool(r["pinned"])} for r in rows]


def turns(user_id, chat_id, limit=100):
    # Newest window, then flipped back to reading order: a long chat has to
    # open on its latest exchange, not its first one.
    rows = db().execute(
        "SELECT role,content,thinking,sources,web FROM turns "
        "WHERE chat_id=(SELECT id FROM chats WHERE id=? AND user_id=?) "
        "ORDER BY rowid DESC LIMIT ?",
        (chat_id, user_id, limit),
    ).fetchall()[::-1]
    out = []
    for r in rows:
        t = {"role": r["role"], "content": r["content"]}
        if r["thinking"]:
            raw = r["thinking"]
            if raw.startswith("["):
                # Model text can start with "[" and still not be our JSON list.
                try:
                    t["thinking"] = json.loads(raw)
                except ValueError:
                    t["thinking"] = raw
            else:
                t["thinking"] = raw
        if r["sources"]:
            t["sources"] = json.loads(r["sources"])
        if r["web"]:
            t["web"] = json.loads(r["web"])
        out.append(t)
    return out


def drop_last_assistant(user_id, chat_id):
    row = db().execute(
        "SELECT rowid FROM turns WHERE chat_id=? AND role='assistant' "
        "AND chat_id IN (SELECT id FROM chats WHERE user_id=?) ORDER BY rowid DESC LIMIT 1",
        (chat_id, user_id),
    ).fetchone()
    if row:
        db().execute("DELETE FROM turns WHERE rowid=?", (row["rowid"],))
        db().commit()


def set_meta(user_id, chat_id, title=None, pinned=None):
    cur = db().execute(
        "SELECT id FROM chats WHERE id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    if not cur:
        return
    if title is not None:
        db().execute("UPDATE chats SET title=? WHERE id=?", (title.strip()[:80] or "New chat", chat_id))
    if pinned is not None:
        db().execute("UPDATE chats SET pinned=? WHERE id=?", (int(bool(pinned)), chat_id))
    db().commit()


def delete(user_id, chat_id):
    # Check ownership first: turns are keyed by chat_id alone, so deleting them
    # after a no-op chat delete would wipe another user's messages.
    row = db().execute(
        "SELECT id FROM chats WHERE id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    if not row:
        return
    db().execute("DELETE FROM turns WHERE chat_id=?", (chat_id,))
    db().execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id, user_id))
    db().commit()
