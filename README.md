# Eris

Minimal chat UI (Claude/ChatGPT-like) with streaming, RAG over ChromaDB,
and a local llama.cpp model server.

## Architecture

The browser side is static HTML, vanilla JS and CSS: `fetch` streams SSE over
POST, `marked.js` renders markdown, `DOMPurify` sanitizes it, and
`highlight.js` handles syntax highlighting.

The backend is FastAPI on uvicorn. It talks to a local model server over
`httpx` and streams deltas back to the browser. Model serving is llama.cpp's
`llama-server` (FILKAN backend) or Ollama; chat streams from the native
`/api/chat` NDJSON endpoint, embeddings come from the OpenAI-compatible
`/v1/embeddings`.

ChromaDB runs in Docker and handles document retrieval.

```
browser ──SSE──▶ FastAPI ──httpx──▶ model server (/api/chat, /v1/embeddings)
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

## Windows

Requires Python 3.10+ (the code uses `X | Y` type syntax) and Docker Desktop
if you want ChromaDB/SearXNG in containers.

```powershell
Copy-Item .env.example .env     # set MODEL_PATH=C:\path\to\model.gguf
.\start.ps1
```

`start.ps1` is the twin of `start.sh`: it creates `.venv` (as
`.venv\Scripts\python.exe`), installs deps, brings up docker services, starts
`llama-server` when `MODEL_PATH` is set, then runs uvicorn. Ctrl-C stops what
it started. If PowerShell blocks the script, allow it for this session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Model server on its own:

```powershell
.\run-model.ps1 C:\path\to\model.gguf
```

`llama-server` must be on `PATH` (a Windows build of llama.cpp). Set
`LLAMA_BIN` to the full path of `llama-server.exe` if it is not.

Manual setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose up -d
.\run-model.ps1 C:\path\to\model.gguf
.\.venv\Scripts\python.exe -m uvicorn app.api:app --reload --port 9000
```

If `docker compose up -d` fails with `ports are not available` on `8000`, some
other process owns ChromaDB's host port. `CHROMA_PORT` in `.env` controls the
mapping (`"${CHROMA_PORT:-8000}:8000"` in `docker-compose.yml`) and the app
reads the same variable, so change it in one place:

```powershell
# .env
CHROMA_PORT=8001
```

```powershell
docker compose up -d
docker compose ps
```

## Notes

- `/api/chat` accepts `{messages, use_rag}` and streams SSE:
  `{"type":"sources"|"delta"|"error", ...}` then `[DONE]`.
- Chat calls `MODEL_BASE_URL` minus the `/v1` suffix, on the native `/api/chat`
  route: NDJSON with a `message` object per line. Only the `/v1` route set is
  OpenAI-shaped, and only embeddings use it.
- RAG uses `MODEL_BASE_URL/v1/embeddings`; the llama-server must be started
  with `--embeddings`.
- Documents are chunked (~800 chars, 100 overlap) and stored with a `source`
  metadata field, a `doc_id`, and each chunk's ordinal `n`; retrieval returns
  top-`RAG_TOP_K` chunks.
- Settings and the Library are dialogs over the chat page, both opened from a
  sidebar row (or `/#settings`, `/#library`), not routes of their own. The shell —
  scrim, sheet, section rail, focus handling — is `static/dialog.css` and
  `static/dialog.js`; `static/settings.html` and `static/library.html` are
  redirects into the matching hash. Esc cancels a pending delete, then closes.
- The Library lists the indexed documents, opens one document's first 50 chunks,
  deletes a document with its chunks, and takes an upload. `POST
  /api/documents/upload` indexes a file, `GET /api/documents` lists them,
  `GET /api/documents/{doc_id}` returns one document, `DELETE
  /api/documents/{doc_id}` removes it. Uploading a filename that is already
  indexed replaces that document: the new copy is indexed first, then the
  previous copy's chunks and row are retired (including pre-migration chunks
  with the same `source`), and the response reports `replaced`.
- Smoke the document path against a running server:
  `.venv\Scripts\python.exe tests/smoke_library.py --email you@example.com --password ...`
- Health dots in the sidebar poll `/api/health`.