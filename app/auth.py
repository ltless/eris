"""Single-file sqlite auth: users, password hashes, and session tokens.

Fine for one box and one operator. Sharing it across instances would need a real
database and signed tokens instead of a sessions table.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
import time

from .store import open_db, path_from_env

SESSION_TTL = 7 * 24 * 3600

# The cookie the session token travels in. The browser sends it under this name
# and the routes set and clear it under the same one, so it is stated here
# rather than pinned by a handler's parameter name.
SESSION_COOKIE = "session"

# The cost of one password hash. Reading it back has to use the same number, or
# every stored password stops verifying, so both callers read it here.
PW_ITERATIONS = 100_000

# The shortest password this app will store. Stated once so the rule the
# register route enforces and the one the change route enforces cannot drift.
MIN_PASSWORD = 6

# The columns added to `users` after the table was first created. The table's
# own declaration and the `ALTER TABLE` sweep both read this one list, so a new
# one cannot reach a fresh database and miss an existing one.
LATE_COLUMNS = ("avatar", "cover", "prefs")
_LATE_SQL = ", ".join(f"{name} TEXT" for name in LATE_COLUMNS)

SCHEMA = (
    "CREATE TABLE users("
    "id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
    "email TEXT UNIQUE NOT NULL COLLATE NOCASE, "
    f"pw TEXT NOT NULL, created INTEGER NOT NULL, {_LATE_SQL});"
    "CREATE TABLE sessions("
    "token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
    "expires INTEGER NOT NULL);"
)

_db = None


def db_path():
    return path_from_env("AUTH_DB", "database/users.db")


def _migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for column in LATE_COLUMNS:
        if column not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")


def db():
    global _db
    _db = open_db(_db, db_path(), SCHEMA, _migrate)
    return _db


def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PW_ITERATIONS).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    try:
        salt, h = stored.split("$")
    except ValueError:
        return False
    cand = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PW_ITERATIONS).hex()
    return hmac.compare_digest(cand, h)


def create_user(name, email, pw):
    email = email.strip().lower()
    if not name.strip() or "@" not in email or len(pw) < MIN_PASSWORD:
        return None, f"name/email/password invalid (min {MIN_PASSWORD} chars)"
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


def user_row(user_id):
    """One user's own row. The whole row, rather than a profile, because a
    caller that has just changed a column needs to read the rest back."""
    return db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


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


# Where each picture of an account is kept. `avatar` and `cover` are columns on
# the user row; the model picture belongs next to the model choice and the
# prompt, so it is a key in the prefs blob. The column name cannot be bound as a
# parameter, which is the other reason this mapping exists: it pins the name to
# a literal rather than trusting the caller.
MEDIA_SLOTS = {"avatar": "avatar", "cover": "cover", "model": "prefs"}
MODEL_AVATAR = "model_avatar"


def set_media(user_id, kind, url):
    slot = MEDIA_SLOTS.get(kind)
    if slot is None:
        raise ValueError(f"unknown media kind: {kind}")
    if slot == "prefs":
        # A None url clears the key instead of storing a null.
        def change(prefs):
            if url:
                prefs[MODEL_AVATAR] = url
            else:
                prefs.pop(MODEL_AVATAR, None)

        update_prefs(user_id, change)
        return
    db().execute(f"UPDATE users SET {slot}=? WHERE id=?", (url, user_id))
    db().commit()


def media_url(user, kind):
    """Where this account's picture of that kind is, or `None`."""
    if MEDIA_SLOTS.get(kind) == "prefs":
        return (user.get("prefs") or {}).get(MODEL_AVATAR)
    return user.get(kind)


def update_prefs(user_id, change):
    """Read, change, and write the prefs blob as one step.

    Two callers each doing their own read around a shared blob kept erasing
    the other's key: the last writer won with a copy taken before the other's
    change had landed.
    """
    row = db().execute("SELECT prefs FROM users WHERE id=?", (user_id,)).fetchone()
    prefs = {}
    if row and row["prefs"]:
        try:
            prefs = json.loads(row["prefs"])
        except ValueError:
            pass
    change(prefs)
    db().execute("UPDATE users SET prefs=? WHERE id=?", (json.dumps(prefs), user_id))
    db().commit()
    return prefs


def change_password(user_id, old_pw, new_pw):
    if len(new_pw) < MIN_PASSWORD:
        return None, f"password too short (min {MIN_PASSWORD} chars)"
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
