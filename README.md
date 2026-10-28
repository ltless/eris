# Eris

Minimal chat UI (Claude/ChatGPT-like) with streaming, a local llama.cpp model
server, and a tool loop that searches the web and cites what it actually
retrieved.

## Architecture

The browser side is static HTML, vanilla JS and CSS: `fetch` streams SSE over
POST, `marked.js` renders markdown, `DOMPurify` sanitizes it, and
`highlight.js` handles syntax highlighting.

The backend is FastAPI on uvicorn. It talks to a local model server over
`httpx` and streams deltas back to the browser. Model serving is llama.cpp's
`llama-server` (FILKAN backend) or Ollama; chat streams from the native
`/api/chat` NDJSON endpoint.

SearXNG runs in Docker and serves web search.

```
browser ──SSE──▶ FastAPI ──httpx──▶ model server (/api/chat)
                     │                  └──▶ SearXNG (web search)
                     └──▶ app/runtime/   one execution loop for chat and search
```

Every turn runs through the same loop and the same tool registry.

## Setup

One command (creates venv, starts SearXNG + llama-server + app):

```bash
cp .env.example .env          # set MODEL_PATH=/path/to/model.gguf
./start.sh
```

`start.sh` auto-starts whichever pieces it can: Docker SearXNG, `llama-server`
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
if you want SearXNG in a container.

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

## Notes

- `/api/chat` accepts `{messages}` and streams SSE:
  `{"type":"sources"|"delta"|"error", ...}` then `[DONE]`.
- Chat calls `MODEL_BASE_URL` minus the `/v1` suffix, on the native `/api/chat`
  route: NDJSON with a `message` object per line.
- Settings is a dialog over the chat page, opened from a sidebar row (or
  `/#settings`), not a route of its own. The shell — scrim, sheet, section rail,
  focus handling — is `static/dialog.css` and `static/dialog.js`. The old
  `static/settings.html` is a redirect into the hash. Esc cancels a pending
  step, then closes.
- Health dots in the sidebar poll `/api/health`, which reports the model server
  and SearXNG.
- Tests: `.venv\Scripts\python.exe tests/run_all.py` runs every suite and
  prints one total. None of them need Docker or a model.
- A tool that changes something needs an approval bound to that exact call,
  and a run that hits one stops there rather than answering around it.
- API tokens (`TOKENS_DB`) let a script, an editor, or an MCP client act as the
  account without a cookie. Only the hash is stored; the plaintext is returned
  once and has no read-back route. A token is **read scope**: `require_user`
  accepts it, and `require_session` — which everything that changes the account
  stands behind — does not, so a leaked token cannot be escalated into a
  takeover. `GET`/`POST /api/tokens` list and mint, `DELETE /api/tokens/{id}`
  revokes. The Settings dialog's API tokens panel is where they are managed.
- `/mcp` speaks Model Context Protocol over Streamable HTTP as a bearer-token
  endpoint: `initialize` (which issues `Mcp-Session-Id`), `tools/list`, and
  `tools/call` over the tools already registered, with `notifications/*` answered
  `202` and `GET` answered `405` because this server never pushes. Tool execution
  failures come back as `isError: true`; a malformed call or an unknown tool is a
  JSON-RPC error. A tool that needs an approval refuses, because a token cannot
  give one.
