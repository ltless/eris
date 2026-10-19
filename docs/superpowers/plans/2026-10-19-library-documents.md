# Library: Documents Implementation Plan

> **Amended after the first build:** the Library ships as a **dialog over the
> chat** (`#libModal` in `static/index.html`), not a route. Task 4's page becomes
> the dialog plus its sidebar opener, `static/library.html` is reduced to a
> redirect into `/#library`, and its check now asserts the dialog markup in the
> chat page. The task list below is the historical record of that build.

> **Second amendment (re-index):** uploading a filename that is already indexed
> now replaces it. Task 3's response gains `replaced`, and the check that asserted
> two rows for one name is replaced by three checks around retiring the previous
> copy. The spec's scope amendment has the reasoning.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Library page that lists indexed documents, shows their chunks, deletes them, and uploads new ones.

**Architecture:** A new `app/library.py` owns `library.db`, one row per upload, the same module shape as `app/auth.py` and `app/chats.py`. `app/rag.py` writes `doc_id` and chunk ordinal `n` into Chroma metadata and gains list, find, chunks, and delete functions that also fold pre-migration chunks into legacy rows. `app/api.py` exposes four routes. A new `/library.html` page plus a sidebar entry reach it.

**Tech Stack:** FastAPI, sqlite3, chromadb, vanilla HTML/CSS/JS, the repo's hand-rolled `check_*` test runner.

**Spec:** `docs/superpowers/specs/2026-10-19-library-documents-design.md`

## Global Constraints

- No new dependency. `requirements.txt` stays as it is, except pinning `chromadb` only if the installed version lacks the calls in Task 1.
- Tests are runnable scripts, not pytest. Each check is a module-level `check_*` function, and the file ends with the existing `sorted(globals())` runner. Run: `.venv\Scripts\python.exe tests\test_library.py`. Expected output is one `ok  <name>` line per check and exit code 0.
- SQLite modules follow `app/chats.py`: `db_path()` reads an env var, `db()` holds a module-level `_db`, connects with `check_same_thread=False`, sets `row_factory = sqlite3.Row`, and creates the table when the file is fresh.
- Every read and write is scoped by `user_id`. A foreign or unknown id returns 404, never 403 and never another account's text.
- Limit values are exact: 25MB upload cap (unchanged, `MAX_DOC_BYTES`), 50 chunks returned by inspect, 2000 chunks per Chroma sweep page.
- UI copy is fixed here and copied verbatim: page title `Library`, upload action `Index a file`, inspect action `Open`, delete action `Delete`, confirm prompt `Delete 42 chunks?` (number from the row), confirm buttons `Delete` and `Cancel`, count line `6 documents · 412 chunks`, empty state `No documents indexed yet.`, legacy meta `indexed before this page existed`.
- Page styling uses `static/style.css` tokens only (`--sp-*`, `--border`, `--radius`, `--bg-soft`, `--muted`, `--text`, `--hover`, `--danger`), matching the Settings page pattern. No new colors, no shadows, no gradients.
- Nothing outside the Library work: chat, media, auth, and the settings pages keep their current behavior.

## Review Focus

Five inputs the spec implies but no single task's happy-path test covers. Each one gets a check in the task that owns the code.

1. A filename with a slash, a quote, a space, or non-ASCII characters. It must list, inspect, and delete like any other, and must not break a URL or a `where` clause.
2. The same filename uploaded twice, then both deleted. Expect two rows, two chunk sets, and nothing orphaned in Chroma.
3. The embedding endpoint or Chroma failing during an upload. Expect a clear 502 or 503 and no half-written row in `documents`.
4. A document with one chunk, and one with more chunks than the inspect cap. Expect the single chunk to render, and the capped read to still report the true total.
5. A second account and a signed-out session reaching document ids. Expect 404 and 401, and never another account's text.

---

### Task 1: The documents store

**Files:**
- Create: `app/library.py`
- Create: `tests/test_library.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `library.db_path() -> Path` (env `LIBRARY_DB`, default `library.db`)
  - `library.db() -> sqlite3.Connection`
  - `library.add(user_id: int, doc_id: str, name: str, size: int, chunks: int) -> dict`
  - `library.list_docs(user_id: int) -> list[dict]`
  - `library.get(user_id: int, doc_id: str) -> dict | None`
  - `library.delete(user_id: int, doc_id: str) -> None`
  - A row dict is `{"id", "name", "bytes", "chunks", "created"}`, `created` in epoch seconds.

- [ ] **Step 1: Confirm the installed Chroma supports what this needs**

Run: `.venv\Scripts\python.exe -c "import chromadb, inspect; from chromadb.api.models.Collection import Collection as C; print(chromadb.__version__); print(inspect.signature(C.get)); print(inspect.signature(C.delete))"`

Expected: both signatures accept `where`, and `get` accepts `limit`. If either does not, pin a version that does in `requirements.txt` before continuing. This is the only place the Chroma version is decided.

- [ ] **Step 2: Write the failing checks**

In `tests/test_library.py`, with the imports and the runner tail copied from `tests/test_media.py`, plus the repo root for the source-level check in Task 4:

```python
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[1]

from app import library
```


```python
class Store:
    """Temp LIBRARY_DB, module reloaded so the table is created fresh."""
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        os.environ["LIBRARY_DB"] = os.path.join(self.dir, "library.db")
        library._db = None
    def close(self):
        if library._db is not None:
            library._db.close()
            library._db = None
        os.environ.pop("LIBRARY_DB", None)


def check_add_and_list_are_scoped_by_user():
    """A row is invisible to every other account."""
    s = Store()
    try:
        library.add(1, "a1", "notes.txt", 120, 3)
        library.add(2, "b1", "other.txt", 10, 1)
        mine = library.list_docs(1)
        assert [r["id"] for r in mine] == ["a1"], mine
        assert mine[0]["name"] == "notes.txt" and mine[0]["chunks"] == 3
        assert library.get(1, "b1") is None, "a foreign id resolved"
        assert library.get(1, "a1")["bytes"] == 120
    finally:
        s.close()


def check_delete_removes_only_that_row():
    s = Store()
    try:
        library.add(1, "a1", "notes.txt", 120, 3)
        library.add(1, "a2", "more.txt", 90, 2)
        library.delete(1, "a1")
        assert [r["id"] for r in library.list_docs(1)] == ["a2"]
        assert library.get(1, "a1") is None
    finally:
        s.close()
```

- [ ] **Step 3: Run them and watch them fail**

Run: `.venv\Scripts\python.exe tests\test_library.py`
Expected: `FAIL check_add_and_list_are_scoped_by_user: No module named 'app.library'`.

- [ ] **Step 4: Implement `app/library.py`**

Signature: `add(user_id: int, doc_id: str, name: str, size: int, chunks: int) -> dict`, `list_docs(user_id: int) -> list[dict]`, `get(user_id: int, doc_id: str) -> dict | None`, `delete(user_id: int, doc_id: str) -> None`, all in `app/library.py`.

Table, created on a fresh file: `documents(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, bytes INTEGER NOT NULL, chunks INTEGER NOT NULL, created INTEGER NOT NULL)`. `created` comes from `int(time.time())`. `list_docs` orders by `created DESC, rowid DESC`. Follow the connection and `db_path()` pattern in `app/chats.py`.

- [ ] **Step 5: Run the checks again**

Run: `.venv\Scripts\python.exe tests\test_library.py`
Expected: `ok  check_add_and_list_are_scoped_by_user` and `ok  check_delete_removes_only_that_row`, exit code 0.

- [ ] **Step 6: Commit**

```bash
git add app/library.py tests/test_library.py
git commit -m "add the documents store"
```

---

### Task 2: Chroma metadata, listing, chunks, and delete

**Files:**
- Modify: `app/rag.py`
- Modify: `tests/test_library.py`

**Interfaces:**
- Consumes: `library.get`, `library.list_docs`, `library.delete` from Task 1.
- Produces:
  - `rag.add_document(user_id: int, source: str, text: str, doc_id: str | None = None) -> int`
  - `rag.list_documents(user_id: int) -> list[dict]` — table rows plus legacy rows, newest first, legacy last. A row adds `"legacy": bool`, and a legacy row has `bytes: None`, `created: None`, and `id = legacy_id(source)`.
  - `rag.find_document(user_id: int, doc_id: str) -> dict | None`
  - `rag.document_chunks(user_id: int, doc_id: str, limit: int = 50) -> list[dict]` — `[{"n": int, "text": str}]`, ordered by `n`, empty list for an unknown id.
  - `rag.delete_document(user_id: int, doc_id: str) -> bool` — `False` when nothing was removed.
  - `rag.legacy_id(source: str) -> str` — `"legacy-" + sha1(source.encode("utf-8")).hexdigest()[:12]`

- [ ] **Step 1: Write the failing checks**

Add a fake Chroma collection to `tests/test_library.py` and monkeypatch `rag.collection`. The matcher is the one part worth writing out, because Chroma's `where` shapes are what the rest of the fake hangs on:

```python
def _match(meta, where):
    if where is None:
        return True
    if "$and" in where:
        return all(_match(meta, w) for w in where["$and"])
    return all(meta.get(k) == v and not isinstance(v, dict) for k, v in where.items())


class FakeCollection:
    """Enough of chromadb's Collection for rag: add, get, delete."""
    def __init__(self):
        self.rows = {}          # id -> {"document", "metadata"}
    def add(self, ids, embeddings=None, documents=None, metadatas=None):
        for i, d, m in zip(ids, documents, metadatas):
            self.rows[i] = {"document": d, "metadata": m}
    def get(self, where=None, limit=None, include=None):
        hits = [r for r in self.rows.values() if _match(r["metadata"], where)]
        if limit:
            hits = hits[:limit]
        return {"documents": [h["document"] for h in hits],
                "metadatas": [h["metadata"] for h in hits]}
    def delete(self, where=None, ids=None):
        for k in [k for k, r in self.rows.items() if _match(r["metadata"], where)]:
            del self.rows[k]
```

Checks, using one fake shared per check and a stub for `rag.embed`:

```python
def check_upload_writes_doc_id_and_ordinal():
    """Chunks carry the document id and their position, in reading order."""
    ... add_document(1, "notes.txt", "alpha beta gamma", doc_id="a1")
    assert [m["n"] for m in fake.rows.values()] == [0]
    assert all(m["metadata"]["doc_id"] == "a1" for m in ...)


def check_legacy_chunks_list_as_one_row_per_source():
    """Pre-migration chunks have no doc_id and must still be visible."""
    ... add one chunk with metadata {"source": "old.txt", "user_id": 1} only
    rows = rag.list_documents(1)
    assert rows[0]["legacy"] is True and rows[0]["id"] == rag.legacy_id("old.txt")
    assert rows[0]["chunks"] == 1 and rows[0]["bytes"] is None


def check_chunks_are_ordered_and_capped():
    """Inspect returns reading order, capped, while the row keeps the real total."""
    ... add a document with 60 chunks under doc_id "a1"
    chunks = rag.document_chunks(1, "a1", limit=50)
    assert len(chunks) == 50 and [c["n"] for c in chunks] == list(range(50))
    assert rag.find_document(1, "a1")["chunks"] == 60, "the cap hid the real total"


def check_delete_by_doc_id_and_by_legacy_source():
    ... delete_document(1, "a1") removes only that document's chunks
    ... delete_document(1, rag.legacy_id("old.txt")) removes the legacy source
    assert rag.delete_document(1, "missing") is False
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv\Scripts\python.exe tests\test_library.py`
Expected: each new check prints `FAIL` naming the missing `rag` attribute.

- [ ] **Step 3: Implement the `app/rag.py` changes**

Signatures as listed in Interfaces. Exact Chroma calls:

- Index: `collection().add(ids=ids, embeddings=vectors, documents=chunks, metadatas=[{"source": source, "user_id": user_id, "doc_id": doc_id, "n": i} for i, _ in enumerate(chunks)])`, writing `doc_id` and `n` only when `doc_id` is not None, so the legacy path is unchanged.
- `list_documents`: `library.list_docs(user_id)` first. Then one sweep, `collection().get(where={"user_id": user_id}, limit=SWEEP_LIMIT, include=["metadatas"])`, dropping metadatas that carry `doc_id`, grouping the rest by `source`, and building one legacy row per source with `chunks` equal to the group size.
- `find_document`: `library.get(user_id, doc_id)`, else resolve a `legacy-...` id by hashing each source seen in the sweep.
- `document_chunks`: `collection().get(where={"$and": [{"user_id": user_id}, {"doc_id": doc_id}]}, include=["documents", "metadatas"])`, sorted by `n`, truncated to `limit`, with `n` defaulting to `0` when a metadata lacks it.
- `delete_document`: legacy id resolves to a source and deletes with `where={"$and": [{"user_id": user_id}, {"source": source}]}`; otherwise `where={"$and": [{"user_id": user_id}, {"doc_id": doc_id}]}`, then `library.delete(user_id, doc_id)`, returning whether the document existed.

- [ ] **Step 4: Run the checks again**

Run: `.venv\Scripts\python.exe tests\test_library.py`
Expected: all checks `ok`, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add app/rag.py tests/test_library.py
git commit -m "index documents with an id and expose list, chunks, delete"
```

---

### Task 3: The document routes

**Files:**
- Modify: `app/api.py` (the upload route at `app/api.py:396` and the routes after it)
- Modify: `tests/test_library.py`

**Interfaces:**
- Consumes: `rag.add_document`, `rag.list_documents`, `rag.find_document`, `rag.document_chunks`, `rag.delete_document` from Task 2; `library` is not imported by `api.py` directly.
- Produces:
  - `POST /api/documents/upload` → `{"id", "name", "chunks"}`
  - `GET /api/documents` → `{"items": [...]}`
  - `GET /api/documents/{doc_id}` → `{"id", "name", "chunks", "bytes", "created", "legacy", "passages": [{"n", "text"}]}`
  - `DELETE /api/documents/{doc_id}` → `{"ok": True}`
  - `INSPECT_CHUNKS = 50` in `app/api.py`

- [ ] **Step 1: Write the failing checks**

Add a TestClient harness that stubs Chroma and the embedder, then the checks. Every `...` below expands to a call on this harness, so the checks stay about their own assertion:

```python
class Api:
    """One registered user, a fake collection, and a stubbed embedder."""
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        os.environ["AUTH_DB"] = os.path.join(self.dir, "users.db")
        os.environ["LIBRARY_DB"] = os.path.join(self.dir, "library.db")
        auth._db = None
        library._db = None
        self.fake = FakeCollection()
        self.old_collection, self.old_embed = rag.collection, rag.embed
        rag.collection = lambda: self.fake
        rag.embed = self.embed                # async, returns [[0.0]] per text
        self.client = TestClient(api.app)
        self.client.post("/api/auth/register", json={...})

    def upload(self, name="notes.txt", text="one line of text"):
        return self.client.post("/api/documents/upload",
                                files={"file": (name, text.encode(), "text/plain")})

    def second_client(self):
        """A separate account in the same temp databases."""
        ...
```

Checks:

```python
def check_upload_row_and_routes():
    """An uploaded file appears in the list, opens, and deletes."""
    h = Api()
    try:
        body = h.upload().json()
        assert body["chunks"] == 1, body
        items = h.client.get("/api/documents").json()["items"]
        assert [i["id"] for i in items] == [body["id"]], items
        opened = h.client.get(f"/api/documents/{body['id']}").json()
        assert opened["passages"][0]["n"] == 0 and opened["passages"][0]["text"]
        assert h.client.delete(f"/api/documents/{body['id']}").json() == {"ok": True}
        assert h.client.get("/api/documents").json()["items"] == []
    finally:
        h.close()


def check_hostile_file_name_survives_the_round_trip():
    """A name with a slash, a quote, and non-ASCII still lists and deletes."""
    h = Api()
    try:
        name = "laporan keuangan \"2026\" (final)/résumé.txt"
        ... upload with that name, then list, open, and delete by the returned id
        assert all four calls returned 200
    finally:
        h.close()


def check_second_upload_of_one_name_keeps_two_documents():
    """Same filename twice is two rows, and deleting both leaves no chunks."""
    h = Api()
    try:
        a, b = h.upload().json()["id"], h.upload().json()["id"]
        assert a != b
        assert len(h.client.get("/api/documents").json()["items"]) == 2
        ... delete both
        assert h.fake.rows == {}, h.fake.rows
    finally:
        h.close()


def check_embedding_failure_leaves_no_row():
    """A 502 from a dead embedder must not leave a half-written document."""
    h = Api()
    try:
        h.fail_embed = True             # rag.embed raises httpx.HTTPError
        assert h.upload().status_code == 502
        assert h.client.get("/api/documents").json()["items"] == []
    finally:
        h.close()


def check_foreign_and_anonymous_access():
    """Another account's id is 404, and no session is 401."""
    h = Api()
    try:
        doc = h.upload().json()["id"]
        other = h.second_client()
        r = other.get(f"/api/documents/{doc}")
        assert r.status_code == 404 and "one line of text" not in r.text, r.text
        anon = TestClient(api.app)
        for call in (anon.get("/api/documents"), anon.get(f"/api/documents/{doc}"),
                     anon.delete(f"/api/documents/{doc}")):
            assert call.status_code == 401, call.text
    finally:
        h.close()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv\Scripts\python.exe tests\test_library.py`
Expected: the new checks `FAIL` with 404 from FastAPI, because the routes do not exist.

- [ ] **Step 3: Implement the routes in `app/api.py`**

Write the row only after indexing returns, so a failed index cannot leave a row with no chunks behind it: generate `doc_id = secrets.token_hex(16)`, call `rag.add_document(u["id"], file.filename or "upload", text, doc_id=doc_id)`, and only on its success call `library.add(...)` with the returned chunk count. On the existing `httpx.HTTPError` / `Exception` branches raise 502 / 503 with the current messages unchanged. `GET`/`DELETE` return 404 with `"unknown document"` when `rag.find_document` misses, and the inspect route caps with `INSPECT_CHUNKS`. Ordering comes from `list_documents`; the route does not re-sort.

- [ ] **Step 4: Run the checks again**

Run: `.venv\Scripts\python.exe tests\test_library.py`
Expected: all checks `ok`, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add app/api.py tests/test_library.py
git commit -m "add document list, inspect, and delete routes"
```

---

### Task 4: The Library page and its sidebar entry

**Files:**
- Create: `static/library.html`, `static/library.css`, `static/library.js`
- Modify: `static/index.html` (the sidebar block at `static/index.html:40-48`)
- Modify: `tests/test_library.py`

**Interfaces:**
- Consumes: the four routes from Task 3.
- Produces: the page at `/library.html`, linked from the sidebar row with `id="sideLibrary"`.

- [ ] **Step 1: Write the failing check**

A source-level check, since the repo has no browser test and this pins real drift:

```python
def check_sidebar_links_the_library_page():
    """The entry exists, links the page, and the page loads its own assets."""
    side = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert 'id="sideLibrary"' in side and "/library.html" in side
    page = (ROOT / "static/library.html").read_text(encoding="utf-8")
    assert "/library.css" in page and "/library.js" in page
    assert 'id="docList"' in page and 'id="docInput"' in page
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv\Scripts\python.exe tests\test_library.py`
Expected: `FAIL check_sidebar_links_the_library_page: library.html`.

- [ ] **Step 3: Build the page**

`static/library.html`: the Settings head pattern (theme script, Plus Jakarta Sans, `style.css`, then `library.css`), a `.lib-head` with the back link `← Eris` and an `h1` reading `Library`, a toolbar with the `Index a file` action and the filter input, a count line element, a `ul#docList`, and a hidden `input#docInput` with `type="file"`.

`static/library.css`: tokens only. Toolbar sits on one row with `--sp-4` gaps and wraps below 720px. Each row is a hairline-separated block with `--sp-4` padding: name, meta line in `--muted` at 12px, actions on the trailing edge. Passages render in a `--bg-soft` block at 12.5px with `--sp-2` gaps. Action buttons reuse the `.s-btn` look from `settings.css` (copy the rule, do not import the file). 44px targets under `@media (pointer: coarse)`, and focus rings on every action.

`static/library.js`: fetch the list on load and paint rows; `Open` toggles a passage block and sets `aria-expanded`; `Delete` swaps the row's actions for the confirm pair, and `DELETE /api/documents/{id}` on confirm, repainting the list after. Upload posts the file to `/api/documents/upload`, then repaints and shows the returned chunk count. Server messages render verbatim in the row or toolbar on failure, and 401 redirects to `/auth.html`, matching `settings.js`.

Copy comes from Global Constraints verbatim. The count line counts documents and sums `chunks`.

- [ ] **Step 4: Add the sidebar entry**

In `static/index.html`, between the `side-new` button and the `side-title` History label, add a link using the existing row anatomy:

```html
<a class="side-row" id="sideLibrary" href="/library.html">
  <span class="ic"><svg viewBox="0 0 24 24"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10v16H5.5A1.5 1.5 0 0 1 4 18.5z"/><path d="M10 4h8.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H10z"/></svg></span>
  <span class="lbl">Library</span>
</a>
```

- [ ] **Step 5: Run the checks again**

Run: `.venv\Scripts\python.exe tests\test_library.py`
Expected: all checks `ok`, exit code 0.

- [ ] **Step 6: Confirm the four states render**

Open `http://127.0.0.1:9000/library.html` and check, at desktop width and below 720px: the empty state with no documents, a populated list with the count line, a row expanded to its passages, and a delete confirmation. No check in this repo can assert that, because there is no browser tool in this session. Report what you see, and fix a wrong state before committing.

- [ ] **Step 7: Commit**

```bash
git add static/library.html static/library.css static/library.js static/index.html tests/test_library.py
git commit -m "add the library page and its sidebar entry"
```

---

### Task 5: Smoke test against a live server

**Files:**
- Create: `tests/smoke_library.py`

**Interfaces:**
- Consumes: every route from Task 3 and the page from Task 4.
- Produces: a runnable script that prints one line per step and exits non-zero on the first failure.

- [ ] **Step 1: Write the script**

`tests/smoke_library.py`, `httpx.Client`, argv `--base http://127.0.0.1:9000 --email --password`. Steps, each printing `ok  <step>`: log in and keep the cookie; upload a generated text file named `smoke notes.txt`; list and assert the id is present; open it and assert at least one passage with `n == 0`; assert the page `/library.html` returns 200 and contains `id="docList"`; delete the id; list and assert it is gone. Report legacy rows with a note line, do not assert them, since they depend on what the operator indexed earlier. Exit 1 with the failing step and the server's message on any non-2xx.

- [ ] **Step 2: Start the stack**

Run: `.venv\Scripts\python.exe -m uvicorn app.api:app --port 9000`
Needs ChromaDB reachable and the embedding endpoint live, because this path really embeds. Leave it running.

- [ ] **Step 3: Run the smoke test**

Run: `.venv\Scripts\python.exe tests\smoke_library.py --email <your email> --password <your password>`
Expected: `ok` for every step, and `smoke passed`. A 502 means the embedder is down; a 503 means Chroma is down; fix the service, not the script.

- [ ] **Step 4: Run the unit checks once more, then commit**

Run: `.venv\Scripts\python.exe tests\test_library.py`

```bash
git add tests/smoke_library.py
git commit -m "smoke test the library documents path"
```

---

### Task 6: Docs

**Files:**
- Modify: `PRODUCT.md`
- Modify: `README.md`

**Interfaces:** none.

- [ ] **Step 1: Update `PRODUCT.md`**

The feature bullet that mentions "PDF and text extraction supported" now also names the Library: indexed documents are listed, inspectable, and removable from `/library.html`. One line, no claim beyond what the routes do.

- [ ] **Step 2: Update `README.md`**

Add the four document routes to whichever route list the file already carries, and the smoke command from Task 5. Match the surrounding style; do not restructure the file.

- [ ] **Step 3: Commit**

```bash
git add PRODUCT.md README.md
git commit -m "document the library documents surface"
```
