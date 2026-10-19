"""Runnable checks: python tests/test_library.py

The Library lists what is indexed, opens a document's chunks, deletes one, and
takes an upload. Chroma is an HTTP service and the model server only matters
for embeddings, so both are replaced here: `rag.collection` becomes an
in-memory fake and `rag.embed` returns a fixed vector. What is left under test
is the document table, the chunk metadata, and the four routes.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[1]

import httpx
from fastapi.testclient import TestClient

from app import api, auth, library, rag


def _match(meta, where):
    """Chroma's two where shapes that this code emits: a plain key set, or
    $and over a list of them."""
    if where is None:
        return True
    if "$and" in where:
        return all(_match(meta, w) for w in where["$and"])
    return all(meta.get(k) == v for k, v in where.items())


class FakeCollection:
    """The parts of chromadb's Collection that rag uses: add, get, delete."""

    def __init__(self):
        self.rows = {}  # id -> {"document", "metadata"}

    def add(self, ids, embeddings=None, documents=None, metadatas=None):
        for i, d, m in zip(ids, documents, metadatas):
            self.rows[i] = {"document": d, "metadata": m}

    def get(self, ids=None, where=None, limit=None, offset=0, include=None):
        if ids is not None:
            found = [(i, self.rows[i]) for i in ids if i in self.rows]
        else:
            found = [(k, r) for k, r in self.rows.items() if _match(r["metadata"], where)]
            if offset:
                found = found[offset:]
            if limit:
                found = found[:limit]
        return {
            "ids": [i for i, _ in found],
            "documents": [r["document"] for _, r in found],
            "metadatas": [r["metadata"] for _, r in found],
        }

    def delete(self, where=None, ids=None):
        if ids is not None:
            for key in ids:
                self.rows.pop(key, None)
            return
        for key in [k for k, r in self.rows.items() if _match(r["metadata"], where)]:
            del self.rows[key]


class FakeRag:
    """A temp LIBRARY_DB plus a fake Chroma and a stubbed embedder."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()
        os.environ["LIBRARY_DB"] = os.path.join(self.dir, "library.db")
        library._db = None
        self.fake = FakeCollection()
        self.fail_embed = False
        self.break_chroma = False
        self._old_collection = rag.collection
        self._old_embed = rag.embed
        rag.collection = self.collection
        rag.embed = self.embed

    def collection(self):
        if self.break_chroma:
            raise RuntimeError("chroma is down")
        return self.fake

    async def embed(self, texts):
        if self.fail_embed:
            raise httpx.HTTPError("embedder is down")
        return [[0.0] * 3 for _ in texts]

    def close(self):
        rag.collection = self._old_collection
        rag.embed = self._old_embed
        if library._db is not None:
            library._db.close()
            library._db = None
        os.environ.pop("LIBRARY_DB", None)


class Api(FakeRag):
    """The same, plus a signed-in client and a temp AUTH_DB."""

    def __init__(self):
        super().__init__()
        os.environ["AUTH_DB"] = os.path.join(self.dir, "users.db")
        auth._db = None
        self.client = TestClient(api.app)
        self.sign_up(self.client, "one@example.com")

    @staticmethod
    def sign_up(client, email):
        r = client.post(
            "/api/auth/register",
            json={"name": "Tester", "email": email, "password": "secret1"},
        )
        assert r.status_code == 200, r.text

    def second_client(self):
        c = TestClient(api.app)
        self.sign_up(c, "two@example.com")
        return c

    def upload(self, name="notes.txt", text="one line of text"):
        return self.client.post(
            "/api/documents/upload",
            files={"file": (name, text.encode("utf-8"), "text/plain")},
        )

    def close(self):
        self.client.close()
        if auth._db is not None:
            auth._db.close()
            auth._db = None
        os.environ.pop("AUTH_DB", None)
        super().close()


# --- the document table ---


def check_add_and_list_are_scoped_by_user():
    """A row is invisible to every other account."""
    s = FakeRag()
    try:
        library.add(1, "a1", "notes.txt", 120, 3)
        library.add(2, "b1", "other.txt", 10, 1)
        mine = library.list_docs(1)
        assert [r["id"] for r in mine] == ["a1"], mine
        assert mine[0]["name"] == "notes.txt" and mine[0]["chunks"] == 3, mine
        assert library.get(1, "b1") is None, "a foreign id resolved"
        assert library.get(1, "a1")["bytes"] == 120, library.get(1, "a1")
    finally:
        s.close()


def check_delete_removes_only_that_row():
    s = FakeRag()
    try:
        library.add(1, "a1", "notes.txt", 120, 3)
        library.add(1, "a2", "more.txt", 90, 2)
        library.delete(1, "a1")
        assert [r["id"] for r in library.list_docs(1)] == ["a2"], library.list_docs(1)
        assert library.get(1, "a1") is None
    finally:
        s.close()


# --- chunk metadata, listing, chunks, delete ---


def check_upload_writes_doc_id_and_ordinal():
    """Chunks carry the document id and their position."""
    h = FakeRag()
    try:
        n = asyncio.run(rag.add_document(1, "notes.txt", "alpha beta gamma", doc_id="a1"))
        assert n == 1, n
        metas = [r["metadata"] for r in h.fake.rows.values()]
        assert metas[0]["doc_id"] == "a1", metas
        assert metas[0]["n"] == 0, metas
        assert metas[0]["source"] == "notes.txt", metas
    finally:
        h.close()


def check_legacy_chunks_list_as_one_row_per_source():
    """Pre-migration chunks have no doc_id and must still be visible."""
    h = FakeRag()
    try:
        h.fake.add(
            ids=["1", "2"],
            documents=["a", "b"],
            metadatas=[{"source": "old.txt", "user_id": 1}, {"source": "old.txt", "user_id": 1}],
        )
        h.fake.add(
            ids=["3"],
            documents=["c"],
            metadatas=[{"source": "other.txt", "user_id": 1}],
        )
        rows = rag.list_documents(1)
        assert [r["id"] for r in rows] == [rag.legacy_id("old.txt"), rag.legacy_id("other.txt")], rows
        assert rows[0]["chunks"] == 2 and rows[0]["bytes"] is None and rows[0]["created"] is None, rows[0]
        assert rows[0]["legacy"] is True and rows[0]["name"] == "old.txt", rows[0]
        # Someone else's legacy chunks are not part of this sweep.
        assert rag.list_documents(2) == [], rag.list_documents(2)
    finally:
        h.close()


def check_chunks_are_ordered_and_capped():
    """Inspect returns reading order, capped, while the row keeps the real total."""
    h = FakeRag()
    try:
        text = "".join(f"block {i} " + "x" * 700 + "\n" for i in range(60))
        n = asyncio.run(rag.add_document(1, "big.txt", text, doc_id="a1"))
        assert n >= 60, n
        library.add(1, "a1", "big.txt", len(text), n)
        chunks = rag.document_chunks(1, "a1", limit=50)
        assert len(chunks) == 50, len(chunks)
        assert [c["n"] for c in chunks] == list(range(50)), chunks[:3]
        assert chunks[0]["text"], chunks[0]
        assert rag.find_document(1, "a1")["chunks"] == n, "the cap hid the real total"
    finally:
        h.close()


def check_single_chunk_document_opens():
    h = FakeRag()
    try:
        n = asyncio.run(rag.add_document(1, "one.txt", "just the one", doc_id="a1"))
        library.add(1, "a1", "one.txt", 12, n)
        chunks = rag.document_chunks(1, "a1", limit=50)
        assert len(chunks) == 1 and chunks[0]["text"] == "just the one", chunks
    finally:
        h.close()


def check_delete_by_doc_id_and_by_legacy_source():
    h = FakeRag()
    try:
        asyncio.run(rag.add_document(1, "notes.txt", "alpha", doc_id="a1"))
        library.add(1, "a1", "notes.txt", 5, 1)
        h.fake.add(
            ids=["L1"],
            documents=["old"],
            metadatas=[{"source": "old.txt", "user_id": 1}],
        )
        assert rag.delete_document(1, "a1") is True
        assert library.get(1, "a1") is None
        assert not [r for r in h.fake.rows.values() if r["metadata"].get("doc_id") == "a1"]
        assert rag.delete_document(1, rag.legacy_id("old.txt")) is True
        assert h.fake.rows == {}, h.fake.rows
        assert rag.delete_document(1, "missing") is False
    finally:
        h.close()


def check_legacy_row_opens_in_chroma_order():
    h = FakeRag()
    try:
        h.fake.add(
            ids=["1", "2"],
            documents=["first", "second"],
            metadatas=[{"source": "old.txt", "user_id": 1}, {"source": "old.txt", "user_id": 1}],
        )
        chunks = rag.document_chunks(1, rag.legacy_id("old.txt"), limit=50)
        assert [c["text"] for c in chunks] == ["first", "second"], chunks
        assert [c["n"] for c in chunks] == [0, 1], chunks
        assert rag.find_document(1, rag.legacy_id("old.txt"))["chunks"] == 2
    finally:
        h.close()


# --- the routes ---


def check_upload_row_and_routes():
    """An uploaded file appears in the list, opens, and deletes."""
    h = Api()
    try:
        r = h.upload()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["chunks"] == 1, body
        assert body["name"] == "notes.txt", body
        assert body["id"], body
        items = h.client.get("/api/documents").json()["items"]
        assert [i["id"] for i in items] == [body["id"]], items
        assert items[0]["legacy"] is False and items[0]["chunks"] == 1, items[0]
        assert items[0]["bytes"] == len("one line of text"), items[0]
        opened = h.client.get(f"/api/documents/{body['id']}")
        assert opened.status_code == 200, opened.text
        assert opened.json()["passages"][0]["n"] == 0, opened.json()
        assert opened.json()["passages"][0]["text"], opened.json()
        assert h.client.delete(f"/api/documents/{body['id']}").json() == {"ok": True}
        assert h.client.get("/api/documents").json()["items"] == []
        assert h.fake.rows == {}, h.fake.rows
    finally:
        h.close()


def check_hostile_file_name_survives_the_round_trip():
    """A name with a slash, a space, and non-ASCII still lists, opens, and
    deletes. A quote never reaches the server as a quote: the multipart
    transport escapes it to %22 first, so the round trip is asserted, not the
    exact spelling."""
    h = Api()
    try:
        name = 'laporan keuangan "2026" (final)/r\u00e9sum\u00e9.txt'
        r = h.upload(name=name, text="halo dunia")
        assert r.status_code == 200, r.text
        doc = r.json()
        assert "/" in doc["name"] and "final" in doc["name"], doc["name"]
        assert "\u00e9" in doc["name"], doc["name"]
        listed = h.client.get("/api/documents").json()["items"]
        assert [i["id"] for i in listed] == [doc["id"]], listed
        assert h.client.get(f"/api/documents/{doc['id']}").status_code == 200
        assert h.client.delete(f"/api/documents/{doc['id']}").status_code == 200
        assert h.client.get("/api/documents").json()["items"] == []
    finally:
        h.close()


def check_reupload_replaces_the_previous_copy():
    """Same filename twice is one row: the second upload retires the first, so
    retrieval cannot return the same text twice."""
    h = Api()
    try:
        first = h.upload(text="first version").json()
        assert first["replaced"] == 0, first
        second = h.upload(text="second version").json()
        assert second["id"] != first["id"], second
        assert second["replaced"] == 1, second

        items = h.client.get("/api/documents").json()["items"]
        assert [i["id"] for i in items] == [second["id"]], items
        assert items[0]["chunks"] == 1, items[0]

        # The first copy's chunks are gone, not merely hidden behind a row.
        assert len(h.fake.rows) == 1, h.fake.rows
        assert [r["document"] for r in h.fake.rows.values()] == ["second version"]
        assert h.client.get(f"/api/documents/{first['id']}").status_code == 404, "the first copy still opens"
        assert h.client.delete(f"/api/documents/{second['id']}").status_code == 200
        assert h.fake.rows == {}, h.fake.rows
    finally:
        h.close()


def check_reupload_retires_a_legacy_group_of_the_same_name():
    """A pre-migration copy under the same filename goes too: the chunks carry
    no doc_id, so nothing else would ever remove them."""
    h = Api()
    try:
        h.fake.add(
            ids=["L1", "L2"],
            documents=["legacy a", "legacy b"],
            metadatas=[
                {"source": "notes.txt", "user_id": 1},
                {"source": "notes.txt", "user_id": 1},
            ],
        )
        before = h.client.get("/api/documents").json()["items"]
        assert len(before) == 1 and before[0]["legacy"] is True, before

        body = h.upload(text="new body").json()
        assert body["replaced"] == 1, body

        items = h.client.get("/api/documents").json()["items"]
        assert [i["id"] for i in items] == [body["id"]], items
        assert items[0]["legacy"] is False, items[0]
        assert [m.get("doc_id") for m in (r["metadata"] for r in h.fake.rows.values())] == [body["id"]], h.fake.rows
    finally:
        h.close()


def check_reupload_leaves_other_names_and_users_alone():
    """Replacing one name must not touch a sibling document, and never another
    account's copy of the same name."""
    h = Api()
    try:
        other = h.second_client()
        h.upload(name="shared.txt", text="mine v1")
        r = other.post(
            "/api/documents/upload",
            files={"file": ("shared.txt", b"theirs", "text/plain")},
        )
        assert r.status_code == 200, r.text
        theirs = r.json()
        keep = h.upload(name="other.txt", text="keep me").json()

        again = h.upload(name="shared.txt", text="mine v2").json()
        assert again["replaced"] == 1, again

        mine = {i["name"]: i["id"] for i in h.client.get("/api/documents").json()["items"]}
        assert mine["shared.txt"] == again["id"], mine
        assert mine["other.txt"] == keep["id"], mine

        rows = other.get("/api/documents").json()["items"]
        assert [i["id"] for i in rows] == [theirs["id"]], "the other account's copy was retired"
        assert other.get(f"/api/documents/{theirs['id']}").status_code == 200
    finally:
        h.close()


def check_embedding_failure_leaves_no_row():
    """A 502 from a dead embedder must not leave a half-written document."""
    h = Api()
    try:
        h.fail_embed = True
        r = h.upload()
        assert r.status_code == 502, r.text
        assert h.client.get("/api/documents").json()["items"] == []
    finally:
        h.close()


def check_unknown_id_is_404():
    h = Api()
    try:
        assert h.client.get("/api/documents/nope").status_code == 404
        assert h.client.delete("/api/documents/nope").status_code == 404
    finally:
        h.close()


def check_foreign_and_anonymous_access():
    """Another account's id is 404, and no session is 401."""
    h = Api()
    try:
        doc = h.upload().json()["id"]
        other = h.second_client()
        assert other.get("/api/documents").json()["items"] == []
        r = other.get(f"/api/documents/{doc}")
        assert r.status_code == 404, r.text
        assert "one line of text" not in r.text, r.text
        assert other.delete(f"/api/documents/{doc}").status_code == 404
        anon = TestClient(api.app)
        assert anon.get("/api/documents").status_code == 401
        assert anon.get(f"/api/documents/{doc}").status_code == 401
        assert anon.delete(f"/api/documents/{doc}").status_code == 401
        assert h.client.get(f"/api/documents/{doc}").status_code == 200
    finally:
        h.close()


def check_legacy_group_excludes_indexed_chunks():
    """A legacy chunk and an indexed document can share a filename. The legacy
    row counts and opens only the chunks that carry no doc_id."""
    h = FakeRag()
    try:
        asyncio.run(rag.add_document(1, "notes.txt", "indexed text", doc_id="a1"))
        library.add(1, "a1", "notes.txt", 12, 1)
        h.fake.add(
            ids=["L1"],
            documents=["legacy text"],
            metadatas=[{"source": "notes.txt", "user_id": 1}],
        )
        legacy = [r for r in rag.list_documents(1) if r["legacy"]]
        assert len(legacy) == 1, rag.list_documents(1)
        assert legacy[0]["chunks"] == 1, legacy[0]
        assert rag.document_chunks(1, legacy[0]["id"]) == [{"n": 0, "text": "legacy text"}]
    finally:
        h.close()


def check_legacy_delete_leaves_an_indexed_document_alone():
    """Deleting the legacy row must not take the indexed document's chunks."""
    h = FakeRag()
    try:
        asyncio.run(rag.add_document(1, "notes.txt", "indexed text", doc_id="a1"))
        library.add(1, "a1", "notes.txt", 12, 1)
        h.fake.add(
            ids=["L1"],
            documents=["legacy text"],
            metadatas=[{"source": "notes.txt", "user_id": 1}],
        )
        assert rag.delete_document(1, rag.legacy_id("notes.txt")) is True
        assert [r["metadata"].get("doc_id") for r in h.fake.rows.values()] == ["a1"], h.fake.rows
        assert rag.document_chunks(1, "a1") == [{"n": 0, "text": "indexed text"}]
        assert [r["id"] for r in rag.list_documents(1)] == ["a1"], rag.list_documents(1)
    finally:
        h.close()


def check_sweep_finds_legacy_rows_past_the_first_page():
    """A store larger than one sweep page still shows its legacy rows."""
    h = FakeRag()
    try:
        # A full first page of indexed chunks, so an unpaged sweep would come
        # back with nothing but modern chunks.
        ids = [f"m{i}" for i in range(rag.SWEEP_LIMIT)]
        h.fake.add(
            ids=ids,
            documents=["x"] * len(ids),
            metadatas=[{"source": "indexed.txt", "user_id": 1, "doc_id": "a1"} for _ in ids],
        )
        h.fake.add(ids=["L1"], documents=["old"], metadatas=[{"source": "old.txt", "user_id": 1}])
        assert [r["id"] for r in rag.list_documents(1)] == [rag.legacy_id("old.txt")]
        assert rag.document_chunks(1, rag.legacy_id("old.txt")) == [{"n": 0, "text": "old"}]
    finally:
        h.close()


def check_chroma_failure_maps_to_503():
    """A dead vector store is a 503 with a reason, not a bare 500."""
    h = Api()
    try:
        doc = h.upload().json()["id"]
        h.break_chroma = True
        r = h.client.get("/api/documents")
        assert r.status_code == 503, f"{r.status_code} {r.text}"
        assert "ChromaDB" in r.json()["detail"], r.text
        assert h.client.get(f"/api/documents/{doc}").status_code == 503
        assert h.client.delete(f"/api/documents/{doc}").status_code == 503
    finally:
        h.close()


# --- the dialog ---


def check_sidebar_opens_the_library_dialog():
    """The entry opens the dialog, the dialog is in the chat page, and the old
    standalone page carries no content of its own any more."""
    side = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert 'id="sideLibrary"' in side, "no library entry in the sidebar"
    assert '<button class="side-row" id="sideLibrary"' in side, "the entry is not a dialog opener"
    assert 'id="libModal"' in side and 'role="dialog"' in side, "no dialog in the chat page"
    assert 'aria-modal="true"' in side, "the dialog does not claim modality"
    assert "/library.css" in side and "/library.js" in side, "the chat page skips the dialog assets"
    assert 'id="docList"' in side and 'id="docInput"' in side, "the dialog lost its list or input"

    page = (ROOT / "static/library.html").read_text(encoding="utf-8")
    assert 'id="docList"' not in page, "the standalone page still renders its own list"
    assert "/#library" in page, "the old route does not redirect into the dialog"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("check_"):
            continue
        try:
            fn()
            print(f"ok  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
