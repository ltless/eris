# Eris

Minimal chat UI (Claude/ChatGPT-like) with streaming, RAG over ChromaDB,
and a local llama.cpp model server.

## Architecture

- **Frontend**: static HTML + vanilla JS + CSS. `fetch` streaming (SSE over
  POST). `marked.js` renders markdown, `DOMPurify` sanitizes, `highlight.js`
  highlights code.
- **Backend**: FastAPI + uvicorn. Proxies to an OpenAI-compatible model
  endpoint via `httpx`, streams deltas back to the browser.
- **Model**: llama.cpp `llama-server` (FILKAN backend) exposing
  `/v1/chat/completions` and `/v1/embeddings`.
- **Vector DB**: ChromaDB (Docker) for document retrieval.

```
browser ──SSE──▶ FastAPI ──httpx──▶ llama-server (/v1)
                     └──▶ ChromaDB (embeddings + retrieval)
```

## Setup

One command (creates venv, starts ChromaDB + llama-server + app):

```bash
cp .env.example .env          # set MODEL_PATH=/path/to/model.gguf
./start.sh
```

`start.sh` auto-starts whichever pieces it can: Docker ChromaDB, `llama-server`
(if `MODEL_PATH` is set and the binary exists), then uvicorn. Ctrl-C stops all.
If `MODEL_PATH` is empty it assumes a model server is already up at
`MODEL_BASE_URL`.

Manual setup:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d
./run-model.sh /path/to/model.gguf
uvicorn app.api:app --reload --port 9000
```

Open http://localhost:9000

## Notes

- `/api/chat` accepts `{messages, use_rag}` and streams SSE:
  `{"type":"sources"|"delta"|"error", ...}` then `[DONE]`.
- RAG uses `MODEL_BASE_URL/v1/embeddings`; the llama-server must be started
  with `--embeddings`.
- Documents are chunked (~800 chars, 100 overlap) and stored with a `source`
  metadata field; retrieval returns top-`RAG_TOP_K` chunks.
- Health dots in the sidebar poll `/api/health`.