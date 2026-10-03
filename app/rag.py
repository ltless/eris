import json
import uuid

import chromadb
import httpx

from .config import settings

_client = None


def client():
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    return _client


def collection():
    return client().get_or_create_collection(settings.chroma_collection)


def chat_collection():
    return client().get_or_create_collection(settings.chroma_collection + "-chats")


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


async def add_document(source, text):
    chunks = chunk(text)
    if not chunks:
        return 0
    vectors = await embed(chunks)
    ids = [str(uuid.uuid4()) for _ in chunks]
    collection().add(
        ids=ids,
        embeddings=vectors,
        documents=chunks,
        metadatas=[{"source": source} for _ in chunks],
    )
    return len(chunks)


async def retrieve(query, top_k=None):
    top_k = top_k or settings.rag_top_k
    if not query.strip():
        return []
    vectors = await embed([query])
    res = collection().query(query_embeddings=vectors, n_results=top_k)
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    return [{"text": d, "source": m.get("source", "")} for d, m in zip(docs, metas)]


def list_documents():
    data = collection().get(include=["metadatas"])
    counts = {}
    for m in data.get("metadatas") or []:
        src = m.get("source", "")
        counts[src] = counts.get(src, 0) + 1
    return [{"source": k, "chunks": v} for k, v in sorted(counts.items())]


def delete_document(source):
    collection().delete(where={"source": source})


def save_turn(user_id, chat_id, role, text, thinking=None, sources=None):
    if not text.strip():
        return
    import time
    # ponytail: 1-dim dummy vector; we only filter by metadata via get().
    # Upgrade to real embeddings when semantic recall over chats is needed.
    meta = {"user_id": user_id, "chat_id": chat_id, "role": role, "ts": int(time.time() * 1000)}
    if thinking:
        meta["thinking"] = thinking[:8000]
    if sources:
        meta["sources"] = json.dumps(sources[:8])
    chat_collection().add(
        ids=[str(uuid.uuid4())],
        embeddings=[[0.0]],
        documents=[text],
        metadatas=[meta],
    )


def chat_turns(user_id, chat_id, limit=100):
    data = chat_collection().get(
        where={"$and": [{"user_id": {"$eq": user_id}}, {"chat_id": {"$eq": chat_id}}]},
        include=["documents", "metadatas"],
        limit=limit,
    )
    docs = data.get("documents") or []
    metas = data.get("metadatas") or []
    turns = sorted(
        (
            {"role": m.get("role", "user"), "content": d, "ts": m.get("ts", 0),
             "thinking": m.get("thinking")}
            for d, m in zip(docs, metas)
        ),
        key=lambda t: t["ts"],
    )
    return [
        {
            "role": t["role"],
            "content": t["content"],
            **({"thinking": t["thinking"]} if t.get("thinking") else {}),
            **({"sources": json.loads(t["sources"])} if t.get("sources") else {}),
        }
        for t in turns
    ]


def chat_list(user_id, limit=50):
    data = chat_collection().get(
        where={"user_id": {"$eq": user_id}},
        include=["documents", "metadatas"],
        limit=limit * 10,
    )
    metas = data.get("metadatas") or []
    docs = data.get("documents") or []
    chats = {}
    for m, d in zip(metas, docs):
        cid = m.get("chat_id")
        if not cid:
            continue
        ts = m.get("ts", 0)
        if m.get("role") == "meta":
            chats.setdefault(cid, {"ts": 0, "title": "New chat"})
            chats[cid].update(
                {k: m[k] for k in ("title", "pinned") if k in m},
                ts=max(ts, chats[cid]["ts"]),
            )
            continue
        if cid not in chats or ts >= chats[cid].get("ts", 0):
            chats[cid] = {"ts": ts, "title": chats.get(cid, {}).get("title") or (d or "")[:40] or "New chat",
                          "pinned": chats.get(cid, {}).get("pinned", False)}
    ordered = sorted(chats.items(), key=lambda kv: (kv[1].get("pinned", False), kv[1]["ts"]), reverse=True)[:limit]
    return [{"id": k, "title": v["title"], "pinned": bool(v.get("pinned"))} for k, v in ordered]


def chat_set_meta(user_id, chat_id, title=None, pinned=None):
    import time
    cur = chat_collection().get(
        where={"$and": [{"user_id": {"$eq": user_id}}, {"chat_id": {"$eq": chat_id}}, {"role": {"$eq": "meta"}}]},
        include=["metadatas"],
        limit=1,
    )
    metas = cur.get("metadatas") or []
    ids = cur.get("ids") or []
    meta = dict(metas[0]) if metas else {"user_id": user_id, "chat_id": chat_id, "role": "meta"}
    if title is not None:
        meta["title"] = title.strip()[:80] or "New chat"
    if pinned is not None:
        meta["pinned"] = bool(pinned)
    meta["ts"] = int(time.time() * 1000)
    if ids:
        chat_collection().update(ids=[ids[0]], metadatas=[meta])
    else:
        chat_collection().add(ids=[str(uuid.uuid4())], embeddings=[[0.0]], documents=["meta"], metadatas=[meta])


def chat_delete(user_id, chat_id):
    data = chat_collection().get(
        where={"$and": [{"user_id": {"$eq": user_id}}, {"chat_id": {"$eq": chat_id}}]},
        include=[],
        limit=1000,
    )
    ids = data.get("ids") or []
    if ids:
        chat_collection().delete(ids=ids)