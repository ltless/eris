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

