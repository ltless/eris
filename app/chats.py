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
    if not text.strip():
        return
    now = int(time.time() * 1000)
    meta = json.dumps(sources[:MAX_SOURCES]) if sources else None
    if isinstance(thinking, list):
        thinking = json.dumps([t[:MAX_THINKING] for t in thinking if t.strip()])
    elif thinking:
        thinking = thinking[:MAX_THINKING]
    webj = json.dumps(web) if web else None
    db().execute(
        "INSERT INTO turns(chat_id,role,content,thinking,sources,web,ts) VALUES(?,?,?,?,?,?,?)",
        (chat_id, role, text, thinking[:MAX_THINKING] if thinking else None, meta, webj, now),
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
    rows = db().execute(
        "SELECT role,content,thinking,sources,web FROM turns "
        "WHERE chat_id=(SELECT id FROM chats WHERE id=? AND user_id=?) "
        "ORDER BY rowid LIMIT ?",
        (chat_id, user_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        t = {"role": r["role"], "content": r["content"]}
        if r["thinking"]:
            raw = r["thinking"]
            t["thinking"] = json.loads(raw) if raw.startswith("[") else raw
        if r["sources"]:
            t["sources"] = json.loads(r["sources"])
        if r["web"]:
            t["web"] = json.loads(r["web"])
        out.append(t)
    return out


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
    db().execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id, user_id))
    db().execute("DELETE FROM turns WHERE chat_id=?", (chat_id,))
    db().commit()
