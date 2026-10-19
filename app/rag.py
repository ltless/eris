import hashlib
import uuid

import chromadb
import httpx

from . import library
from .config import settings

_client = None

# A sweep is the one call that could pull a whole collection, so it is capped
# per page and bounded in pages.
SWEEP_LIMIT = 2000
SWEEP_PAGES = 10
LEGACY_PREFIX = "legacy-"


def client():
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    return _client


def collection():
    return client().get_or_create_collection(settings.chroma_collection)


async def embed(texts):
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"{settings.model_base_url}/embeddings",
            headers={"Authorization": f"Bearer {settings.model_api_key}"},
            json={"model": settings.embed_model, "input": texts},
        )
        r.raise_for_status()
        return [d["embedding"] for d in r.json()["data"]]


def chunk(text, size=800, overlap=100):
    text = text.strip()
    if not text:
        return []
    step = max(1, size - overlap)
    return [text[i : i + size] for i in range(0, len(text), step) if text[i : i + size].strip()]


async def add_document(user_id, source, text, doc_id=None):
    chunks = chunk(text)
    if not chunks:
        return 0
    vectors = await embed(chunks)
    ids = [str(uuid.uuid4()) for _ in chunks]
    metadatas = []
    for i in range(len(chunks)):
        meta = {"source": source, "user_id": user_id}
        # Only indexed documents carry an id and an ordinal. Chunks written
        # before this change keep the old shape and are folded into legacy
        # rows on read.
        if doc_id:
            meta["doc_id"] = doc_id
            meta["n"] = i
        metadatas.append(meta)
    collection().add(
        ids=ids,
        embeddings=vectors,
        documents=chunks,
        metadatas=metadatas,
    )
    return len(chunks)


async def retrieve(user_id, query, top_k=None):
    top_k = top_k or settings.rag_top_k
    if not query.strip():
        return []
    vectors = await embed([query])
    res = collection().query(query_embeddings=vectors, n_results=top_k, where={"user_id": user_id})
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    return [{"text": d, "source": m.get("source", "")} for d, m in zip(docs, metas)]


def legacy_id(source):
    """Address for a group of chunks that predate document ids. Derived from
    the source so the same row can be recomputed on every sweep."""
    return LEGACY_PREFIX + hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]


def _legacy_groups(user_id):
    """source -> the chunk ids that carry no doc_id, paged.

    Chroma cannot express "this key is absent" in a `where`, and `source` is
    shared with indexed documents that reuse the filename, so a filter on
    `source` alone would reach the wrong chunks. The sweep is what decides
    membership, and these ids are what every later call acts on.
    """
    groups = {}
    offset = 0
    while offset < SWEEP_LIMIT * SWEEP_PAGES:
        res = collection().get(
            where={"user_id": user_id},
            limit=SWEEP_LIMIT,
            offset=offset,
            include=["metadatas"],
        )
        ids = res.get("ids") or []
        for chunk_id, meta in zip(ids, res.get("metadatas") or []):
            if meta and not meta.get("doc_id"):
                groups.setdefault(meta.get("source") or "", []).append(chunk_id)
        if len(ids) < SWEEP_LIMIT:
            break
        offset += SWEEP_LIMIT
    return groups


def _legacy_rows(user_id):
    """One row per source among chunks that carry no doc_id."""
    return [
        {
            "id": legacy_id(source),
            "name": source,
            "bytes": None,
            "chunks": len(ids),
            "created": None,
            "legacy": True,
        }
        for source, ids in _legacy_groups(user_id).items()
    ]


def _legacy_ids(user_id, doc_id):
    for source, ids in _legacy_groups(user_id).items():
        if legacy_id(source) == doc_id:
            return ids
    return None


def list_documents(user_id):
    """Indexed documents first, newest first, then the pre-migration groups."""
    rows = [dict(r, legacy=False) for r in library.list_docs(user_id)]
    return rows + _legacy_rows(user_id)


def find_document(user_id, doc_id):
    row = library.get(user_id, doc_id)
    if row:
        return dict(row, legacy=False)
    for row in _legacy_rows(user_id):
        if row["id"] == doc_id:
            return row
    return None


def document_chunks(user_id, doc_id, limit=50):
    """Chunk text for one document in reading order, capped by the caller."""
    if doc_id.startswith(LEGACY_PREFIX):
        ids = _legacy_ids(user_id, doc_id)
        if not ids:
            return []
        # Legacy chunks have no ordinal, so Chroma's order is the only order
        # they have. The index in that order is what the page can show.
        res = collection().get(ids=ids[:limit], include=["documents"])
        return [{"n": i, "text": t} for i, t in enumerate(res.get("documents") or [])]
    res = collection().get(
        where={"$and": [{"user_id": user_id}, {"doc_id": doc_id}]},
        include=["documents", "metadatas"],
    )
    metas = res.get("metadatas") or []
    docs = res.get("documents") or []
    pairs = sorted(((m.get("n", 0), t) for m, t in zip(metas, docs)), key=lambda p: p[0])
    return [{"n": n, "text": t} for n, t in pairs[:limit]]


def retire_previous(user_id, source, keep=None):
    """Drop the copies a fresh upload of `source` replaces.

    Same name means same document, re-indexed. Both shapes go: the indexed rows
    with that name (there can be more than one from the appending era) and the
    pre-migration chunks carrying that source. Returns how many documents were
    retired.

    `keep` is the row just written for the new copy: it shares the name, so it
    has to be excluded. Called after the new copy is stored, so a failure here
    cannot leave the user with nothing.
    """
    retired = 0
    for row in library.find_by_name(user_id, source):
        if row["id"] == keep:
            continue
        collection().delete(where={"$and": [{"user_id": user_id}, {"doc_id": row["id"]}]})
        library.delete(user_id, row["id"])
        retired += 1

    # A sweep is not needed here: only chunks whose source is this name can
    # belong to the document being replaced, and the ones without a doc_id are
    # exactly the pre-migration copies.
    res = collection().get(
        where={"$and": [{"user_id": user_id}, {"source": source}]},
        include=["metadatas"],
    )
    legacy = [
        chunk_id
        for chunk_id, meta in zip(res.get("ids") or [], res.get("metadatas") or [])
        if meta and not meta.get("doc_id")
    ]
    if legacy:
        collection().delete(ids=legacy)
        retired += 1
    return retired


def discard(user_id, doc_id):
    """Best-effort undo of an upload that could not be finished."""
    try:
        collection().delete(where={"$and": [{"user_id": user_id}, {"doc_id": doc_id}]})
    except Exception:
        pass
    try:
        library.delete(user_id, doc_id)
    except Exception:
        pass


def delete_document(user_id, doc_id):
    """Remove one document's chunks and its row. False when it was not there.

    A legacy id is resolved to the chunk ids the sweep found, never to a bare
    `source` value: a filename may be shared with an indexed document, and
    deleting by source would take that document's chunks with it.
    """
    if doc_id.startswith(LEGACY_PREFIX):
        ids = _legacy_ids(user_id, doc_id)
        if not ids:
            return False
        collection().delete(ids=ids)
        return True
    if not library.get(user_id, doc_id):
        return False
    collection().delete(where={"$and": [{"user_id": user_id}, {"doc_id": doc_id}]})
    library.delete(user_id, doc_id)
    return True

