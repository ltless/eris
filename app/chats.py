"""Chat persistence in sqlite. Turns are replayed, never searched, so no vector store here."""

import base64
import binascii
import json
import time
from dataclasses import dataclass

from .encoding import new_id
from .store import open_db, path_from_env

MAX_THINKING = 8000
# How many source rows a stored turn keeps. A run's own budget bounds what it
# can find, but a turn that searched every round can still collect more rows
# than a replay needs, and the citations of the dropped ones stay in the answer
# text as numbers the reload marks rather than links. The live list has no cap:
# a reader watching the run sees every source it found.
MAX_SOURCES = 8
MAX_IMAGE_BYTES = 4_000_000

# The title a chat carries before anyone has named it. It stands in the sidebar
# as a readable placeholder, so it is a value rather than NULL, and every check
# for "still unnamed" is against this string.
UNTITLED = "New chat"

# How much of a first message becomes a title. `name_from_model` compares the
# stored title against `short_title(source)` to tell the model's name from the
# one the first message wrote, so both readings have to use the same limit as
# the default here.
TITLE_CHARS = 40
# A name someone chose, or the model wrote, may run longer than the first
# message's own title. The two share one bound so a rename cannot outrun a
# listing by a different number.
NAME_CHARS = 80

# The three types a stored image may be, by the bytes it starts with. The type
# is read off the bytes rather than taken from the data URL, so what comes back
# out is what the bytes actually are. The browser side keeps to the same three
# when it encodes, which is what makes this list total.
IMAGE_TYPES = {
    1: ("image/jpeg", b"\xff\xd8\xff"),
    2: ("image/png", b"\x89PNG\r\n\x1a\n"),
    3: ("image/gif", b"GIF8"),
}


def _sniff_media_type(raw):
    """The tag for these bytes, or `None` if they are not an image we store."""
    for tag, (_, magic) in IMAGE_TYPES.items():
        if raw.startswith(magic):
            return tag
    return None


def pack_images(raws):
    """The bytes of several images as one column, or `None` if there are none.

    Framed rather than JSON: the payload is the image itself, and base64 in a
    text column would store a third more than the picture weighs. Each frame is
    a type tag and a big-endian length in front of the bytes.
    """
    kept = []
    total = 0
    for raw in raws:
        tag = _sniff_media_type(raw)
        if tag is None:
            continue
        if total + len(raw) > MAX_IMAGE_BYTES:
            break
        kept.append(len(raw).to_bytes(4, "big") + bytes([tag]) + raw)
        total += len(raw)
    return b"".join(kept) or None


def unpack_images(blob):
    """One turn's stored images as the data URLs the client draws and sends."""
    if not blob:
        return []
    out = []
    at = 0
    while at + 5 <= len(blob):
        size = int.from_bytes(blob[at : at + 4], "big")
        tag = blob[at + 4]
        raw = blob[at + 5 : at + 5 + size]
        if len(raw) != size or tag not in IMAGE_TYPES:
            break
        media, _ = IMAGE_TYPES[tag]
        out.append(f"data:{media};base64,{base64.b64encode(raw).decode()}")
        at += 5 + size
    return out


def decode_data_url(url):
    """A `data:` URL as bytes, or `None` when it is not one we can store."""
    if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
        return None
    head, payload = url.split(",", 1)
    if "base64" not in head:
        return None
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None

def _thinking_from_stored(raw):
    """A stored thinking entry as the browser reads it. Model text can start
    with "[" and still not be our JSON list, so the parse is tried, not taken."""
    if not raw.startswith("["):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# Every column a turn carries beyond the three that are always there, in the
# order they are written: the SQL type it is stored as, and how a stored value
# becomes what a replay draws (`None` for text that is already itself). The
# table, the migration, the insert and the replay all read this one list, so a
# field added here is stored and read back without a second edit.
COLUMNS = (
    ("quote", "TEXT", None),
    ("thinking", "TEXT", _thinking_from_stored),
    ("sources", "TEXT", json.loads),
    ("web", "TEXT", json.loads),
    ("fetches", "TEXT", json.loads),
    ("answers", "TEXT", json.loads),
    ("images", "BLOB", unpack_images),
    ("files", "TEXT", json.loads),
    ("cards", "TEXT", json.loads),
)

_COLUMN_SQL = ", ".join(f"{name} {kind}" for name, kind, _ in COLUMNS)
_COLUMN_NAMES = ", ".join(name for name, _, _ in COLUMNS)
_COLUMN_MARKS = ", ".join("?" for _ in COLUMNS)
_COLUMN_SET = {name for name, _, _ in COLUMNS}
_INSERT_TURN = (
    f"INSERT INTO turns(chat_id,role,content,{_COLUMN_NAMES},ts) "
    f"VALUES(?,?,?,{_COLUMN_MARKS},?)"
)

SCHEMA = (
    "CREATE TABLE chats("
    "id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
    f"title TEXT NOT NULL DEFAULT '{UNTITLED}', "
    "pinned INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL, "
    "instance TEXT NOT NULL DEFAULT '');"
    "CREATE TABLE turns("
    "chat_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
    f"{_COLUMN_SQL}, ts INTEGER NOT NULL);"
    "CREATE INDEX turns_chat ON turns(chat_id);"
)

_db = None


def db_path():
    return path_from_env("CHAT_DB", "database/chats.db")


def _migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
    for name, kind, _ in COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE turns ADD COLUMN {name} {kind}")
    chat_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chats)")}
    if "instance" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN instance TEXT NOT NULL DEFAULT ''")


def db():
    global _db
    _db = open_db(_db, db_path(), SCHEMA, _migrate)
    return _db


@dataclass(frozen=True)
class ChatRun:
    chat_id: str
    user_id: int
    instance: str
    anchor: int
    regenerate: bool = False


def _turn_fields(extra):
    unknown = set(extra) - _COLUMN_SET
    if unknown:
        raise TypeError(f"save_turn got an unknown field: {', '.join(sorted(unknown))}")
    quote, images = extra.get("quote"), extra.get("images")
    thinking = extra.get("thinking")
    if isinstance(thinking, list):
        # Truncating the serialized JSON would leave it unparseable, so trim by
        # whole rounds and capped text until the encoding itself fits.
        # Slots are kept even when empty: their position is the round number the
        # browser draws each card at.
        kept = list(thinking)
        size = len(json.dumps(kept))
        while kept and size > MAX_THINKING:
            over = size - MAX_THINKING
            kept[-1] = kept[-1][: max(len(kept[-1]) - over, 0)]
            if not kept[-1]:
                kept.pop()
            size = len(json.dumps(kept))
        thinking = json.dumps(kept) if kept else None
    elif thinking:
        thinking = thinking[:MAX_THINKING]
    sources = extra.get("sources")
    stored = {
        "quote": quote or None,
        "thinking": thinking or None,
        "sources": json.dumps(sources[:MAX_SOURCES]) if sources else None,
        "images": pack_images(images) if images else None,
    }
    # The rest of `COLUMNS`, read from the same declaration the schema and the
    # reader are built from: every column not singled out above is JSON.
    for name, _, _ in COLUMNS:
        if name not in stored:
            value = extra.get(name)
            stored[name] = json.dumps(value) if value else None
    return tuple(stored[name] for name, _, _ in COLUMNS)


def save_turn(user_id, chat_id, role, text, **extra):
    """Append a turn and return the anchor for its reply."""
    fields = _turn_fields(extra)
    if not chat_id or not (text.strip() or (extra.get("quote") or "").strip()
                           or extra.get("images") or extra.get("files")):
        return None
    now = int(time.time() * 1000)
    with db() as conn:
        conn.execute(
            "INSERT INTO chats(id,user_id,ts,instance) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (chat_id, user_id, now, new_id()),
        )
        owner = conn.execute(
            "SELECT instance FROM chats WHERE id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        if owner is None:
            return None
        turn = conn.execute(_INSERT_TURN, (chat_id, role, text, *fields, now))
        conn.execute("UPDATE chats SET ts=? WHERE id=?", (now, chat_id))
        return ChatRun(chat_id, user_id, owner["instance"], turn.lastrowid)


def _latest_turn(conn, user_id, chat_id):
    return conn.execute(
        "SELECT t.rowid, t.role, c.instance FROM turns t "
        "JOIN chats c ON c.id=t.chat_id WHERE c.id=? AND c.user_id=? "
        "ORDER BY t.rowid DESC LIMIT 1", (chat_id, user_id),
    ).fetchone()


def start_regeneration(user_id, chat_id):
    tail = _latest_turn(db(), user_id, chat_id)
    if tail is None or tail["role"] != "assistant":
        return None
    return ChatRun(chat_id, user_id, tail["instance"], tail["rowid"], regenerate=True)


def finish_run(run, text, **extra):
    """Publish the reply only if the chat still has the turn this run started from."""
    if not text.strip():
        return False
    fields = _turn_fields(extra)
    now = int(time.time() * 1000)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        tail = _latest_turn(conn, run.user_id, run.chat_id)
        if tail is None or (tail["instance"], tail["rowid"]) != (run.instance, run.anchor):
            return False
        conn.execute(_INSERT_TURN, (run.chat_id, "assistant", text, *fields, now))
        if run.regenerate:
            conn.execute("DELETE FROM turns WHERE rowid=?", (run.anchor,))
        conn.execute("UPDATE chats SET ts=? WHERE id=?", (now, run.chat_id))
    return True


def _placeholder(title):
    """Whether a stored title is still the placeholder. One predicate for the
    question, so a chat that may be named and a chat that is named from its
    first message cannot disagree about a title with whitespace around it."""
    return (title or "").strip() == UNTITLED

def untitled(chat_id, user_id=None):
    """True while a chat has never been named: no row yet, or still the
    placeholder the first user message overwrites. A chat the user emptied is
    back to unnamed, so a later message may name it again."""
    row = _title_row(chat_id, user_id)
    return row is None or _placeholder(row["title"])


def short_title(text, limit=TITLE_CHARS):
    """`text` as a one-line title of at most `limit` characters.

    Cut on a word boundary where there is one, because a title that ends
    mid-word reads as broken text rather than as a shortened one.
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip()


def touch_title(chat_id, text, user_id=None, *, instance=None):
    """Name a chat from its first user message, if nothing has named it yet."""
    with db() as conn:
        conn.execute(
            "UPDATE chats SET title=? WHERE id=? AND trim(title)=? "
            "AND (? IS NULL OR user_id=?) AND (? IS NULL OR instance=?)",
            (short_title(text) or UNTITLED, chat_id, UNTITLED,
             user_id, user_id, instance, instance),
        )


def _title_row(chat_id, user_id):
    """The chat's title row, only if it belongs to `user_id` when one is given:
    `chat_id` comes from the client, so a caller that names its user cannot read
    or rename another user's chat."""
    if user_id is None:
        return db().execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
    return db().execute(
        "SELECT title FROM chats WHERE id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()


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
        f"SELECT role,content,{_COLUMN_NAMES} FROM turns "
        "WHERE chat_id=(SELECT id FROM chats WHERE id=? AND user_id=?) "
        "ORDER BY rowid DESC LIMIT ?",
        (chat_id, user_id, limit),
    ).fetchall()[::-1]
    out = []
    for r in rows:
        t = {"role": r["role"], "content": r["content"]}
        for name, _, load in COLUMNS:
            if r[name]:
                t[name] = load(r[name]) if load else r[name]
        out.append(t)
    return out


def name_from_model(user_id, chat_id, title, source, *, instance=None):
    """Write the model's name for a chat, unless a name someone chose is there.

    Both that name and the one it replaces come from the first message, so the
    write is dropped when what is stored is neither: the user can rename the
    chat while the answer is still streaming, and that name wins.
    """
    with db() as conn:
        updated = conn.execute(
            "UPDATE chats SET title=? WHERE id=? AND user_id=? "
            "AND (trim(title)=? OR title=?) AND (? IS NULL OR instance=?)",
            (short_title(title, NAME_CHARS) or UNTITLED, chat_id, user_id, UNTITLED,
             short_title(source), instance, instance),
        )
    return updated.rowcount == 1


def set_meta(user_id, chat_id, title=None, pinned=None):
    cur = db().execute(
        "SELECT id FROM chats WHERE id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    if not cur:
        return
    if title is not None:
        db().execute("UPDATE chats SET title=? WHERE id=?",
                     (short_title(title, NAME_CHARS) or UNTITLED, chat_id))
    if pinned is not None:
        db().execute("UPDATE chats SET pinned=? WHERE id=?", (int(bool(pinned)), chat_id))
    db().commit()


def delete(user_id, chat_id):
    with db() as conn:
        deleted = conn.execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id, user_id))
        if deleted.rowcount:
            conn.execute("DELETE FROM turns WHERE chat_id=?", (chat_id,))
