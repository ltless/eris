"""Single-file sqlite auth: users, password hashes, and session tokens.

Fine for one box and one operator. Sharing it across instances would need a real
database and signed tokens instead of a sessions table.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

SESSION_TTL = 7 * 24 * 3600  # 7 days

_db = None


def db_path():
    return Path(os.getenv("AUTH_DB", "users.db"))


def db():
    global _db
    if _db is None:
        fresh = not db_path().exists()
        _db = sqlite3.connect(db_path(), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        if fresh:
            _db.executescript(
                "CREATE TABLE users("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "email TEXT UNIQUE NOT NULL COLLATE NOCASE, "
                "pw TEXT NOT NULL, created INTEGER NOT NULL, "
                "avatar TEXT, cover TEXT, prefs TEXT);"
                "CREATE TABLE sessions("
                "token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                "expires INTEGER NOT NULL);"
            )
        else:
            cols = {r["name"] for r in _db.execute("PRAGMA table_info(users)")}
            if "avatar" not in cols:
                _db.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
            if "cover" not in cols:
                _db.execute("ALTER TABLE users ADD COLUMN cover TEXT")
            if "prefs" not in cols:
                _db.execute("ALTER TABLE users ADD COLUMN prefs TEXT")
    return _db


def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    try:
        salt, h = stored.split("$")
    except ValueError:
        return False
    cand = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000).hex()
    return hmac.compare_digest(cand, h)


def create_user(name, email, pw):
    email = email.strip().lower()
    if not name.strip() or "@" not in email or len(pw) < 6:
        return None, "name/email/password invalid (min 6 chars)"
    try:
        cur = db().execute(
            "INSERT INTO users(name,email,pw,created) VALUES(?,?,?,?)",
            (name.strip(), email, hash_pw(pw), int(time.time())),
        )
        db().commit()
    except sqlite3.IntegrityError:
        return None, "email already registered"
    return {"id": cur.lastrowid, "name": name.strip(), "email": email}, None


def verify_user(email, pw):
    row = db().execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not row or not check_pw(pw, row["pw"]):
        return None
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    db().execute(
        "INSERT INTO sessions(token,user_id,expires) VALUES(?,?,?)",
        (token, user_id, int(time.time()) + SESSION_TTL),
    )
    db().commit()
    return token


def profile(row):
    prefs = {}
    if row["prefs"]:
        try:
            prefs = json.loads(row["prefs"])
        except ValueError:
            pass
    return {
        "id": row["id"], "name": row["name"], "email": row["email"],
        "avatar": row["avatar"], "cover": row["cover"], "prefs": prefs,
    }


def get_session(token):
    if not token:
        return None
    row = db().execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=? AND s.expires>?",
        (token, int(time.time())),
    ).fetchone()
    if not row:
        return None
    return profile(row)


def set_name(user_id, name):
    db().execute("UPDATE users SET name=? WHERE id=?", (name, user_id))
    db().commit()


def set_media(user_id, kind, url):
    # The column name cannot be bound as a parameter, so it is pinned to a
    # literal set here rather than trusted from the caller.
    column = {"avatar": "avatar", "cover": "cover"}.get(kind)
    if not column:
        raise ValueError(f"unknown media kind: {kind}")
    db().execute(f"UPDATE users SET {column}=? WHERE id=?", (url, user_id))
    db().commit()


def set_prefs(user_id, prefs):
    db().execute("UPDATE users SET prefs=? WHERE id=?", (json.dumps(prefs), user_id))
    db().commit()


def change_password(user_id, old_pw, new_pw):
    if len(new_pw) < 6:
        return None, "password too short (min 6 chars)"
    row = db().execute("SELECT pw FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not check_pw(old_pw, row["pw"]):
        return None, "old password wrong"
    db().execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(new_pw), user_id))
    db().execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    token = new_session(user_id)
    return token, None


def drop_session(token):
    if token:
        db().execute("DELETE FROM sessions WHERE token=?", (token,))
        db().commit()
