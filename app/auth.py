"""ponytail: single-file sqlite auth. ceiling: multi-instance/multi-user scale -> real DB + JWT."""

import hashlib
import hmac
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
                "pw TEXT NOT NULL, created INTEGER NOT NULL);"
                "CREATE TABLE sessions("
                "token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                "expires INTEGER NOT NULL);"
            )
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


def get_session(token):
    if not token:
        return None
    row = db().execute(
        "SELECT u.id,u.name,u.email FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=? AND s.expires>?",
        (token, int(time.time())),
    ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def drop_session(token):
    if token:
        db().execute("DELETE FROM sessions WHERE token=?", (token,))
        db().commit()
