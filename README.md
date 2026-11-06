# Eris

Chat UI with streaming, a local llama.cpp or Ollama server behind it, and a tool
loop that searches the web and cites the pages it actually opened.

Type something, the answer arrives token by token, and the model can search and
read pages mid-answer instead of guessing.

## What's in it

Browser side is plain HTML, JS and CSS. No build step, no bundler. `fetch`
streams SSE, marked renders markdown, DOMPurify cleans it, highlight.js colours
code.

Backend is FastAPI on uvicorn. It talks to the model server with httpx over the
native `/api/chat` route as NDJSON. The `/v1` on `MODEL_BASE_URL` exists only
for the model list.

SearXNG runs in Docker and serves web search. If you already have a model server
running somewhere, point `MODEL_BASE_URL` at it and skip the model half.

```
browser --SSE--> FastAPI --httpx--> model server (/api/chat)
                     |                    +--> SearXNG
                     +--> app/runtime/  one loop for every turn
```

Every turn goes through the same loop in `app/runtime/` and the same tool
registry. Limits live in `app/runtime/budgets.py` because telling a model "search
at most three times" does not work.

## Running it

Linux or macOS:

```bash
cp .env.example .env          # set MODEL_PATH=/path/to/model.gguf
./start.sh
```

Windows:

```powershell
Copy-Item .env.example .env   # set MODEL_PATH=C:\path\to\model.gguf
.\start.ps1
```

Both scripts do the same thing: make a venv, install deps, bring up Docker,
start `llama-server` if `MODEL_PATH` is set, then run uvicorn. Ctrl-C takes down
what they started. Leave `MODEL_PATH` empty and they assume a model server is
already up at `MODEL_BASE_URL`.

Then open http://localhost:9000

If PowerShell refuses the script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Doing it by hand:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d
./run-model.sh /path/to/model.gguf     # only if you want llama.cpp local
uvicorn app.api:app --reload --port 9000
```

Windows needs Python 3.10+ for the `X | Y` type syntax. `llama-server` has to be
on `PATH`, or set `LLAMA_BIN` to the full path of `llama-server.exe`.

`start.ps1` passes `--reload --reload-dir app` to uvicorn, so edits under `app/`
restart it. `start.sh` does not; add the flag yourself if you want it.

## Composer

The box at the bottom has more in it than it looks.

- Model picker. Remembers your choice per account.
- Effort picker. The steps come from the model's own `think` set, asked once per
  model. Some templates drop the tool list when `think` is `false`, so a tool
  round sends the lowest rung the model names instead.
- Ring and panel beside Send. Context used, what the server has loaded, KV
  cost. Read-only, costs no tokens.
- Attach. Images and text files. A photo is shrunk to 1024px and re-encoded as
  JPEG, since a 4MB phone photo is pixels the model will never resolve. A model
  that didn't declare vision shows the chip as refused instead of dropping it
  silently. Text files are read in the browser and capped at 40k characters
  each, and the model gets each one under its own name; the turn stores those
  files as a separate field, so the bubble shows the file's chip and not the
  whole file. A click on that chip opens the text in a modal. What was sent is
  what comes back after a reload: images go into the `turns` row as bytes,
  because the model is handed a data URL and a path would be handed to it as a
  path.

Select text in an answer and you get a quote chip above the box. Send it and the
chip and your typed text stay two separate things, before and after reload. They
are stored as separate fields for that reason.

## Storage

Three sqlite files, all under `database/`. Nothing here is a real database, it is
one box and one operator.

- `users.db` (`AUTH_DB`) accounts, password hashes, sessions. A session cookie
  lasts seven days.
- `chats.db` (`CHAT_DB`) chats and turns. Turns are replayed, never searched, so
  no vector store.
- `tokens.db` (`TOKENS_DB`) API tokens, hash only.

Schema is created on a fresh file and `ALTER`-ed on an old one, so an existing
install keeps its tables.

## API tokens

For scripts and editors that should not hold a cookie. Plaintext
comes back once at creation and there is no read-back route. A token is read
scope: `require_user` accepts it, `require_session` does not, and everything that
changes the account sits behind the second one. A leaked token cannot be turned
into a takeover.

`GET`/`POST /api/tokens` list and mint. `DELETE /api/tokens/{id}` revokes. The
panel is in Settings.

## Tools

Ten built in: `web_search`, `fetch_url`, `weather`, `wayback`, `read_file`,
`crypto`, `crypto_trend`, `equity`, `equity_trend`, `screen`.
`fetch_url` reads PDFs too, and it refuses any address that isn't a public one,
so it can't be pointed at your own network. `weather` and `wayback` read the
open web as well and go through the same guard.

`weather` takes a place by name, geocodes it, and answers with the conditions
now and a few days ahead, from open-meteo. No key and no account. The name it
is given is what a small model happened to type, so a name the geocoder doesn't
know is retried without its administrative prefix before the tool gives up.

`wayback` picks up where `fetch_url` stops: when a page answers 404, or when the
user asks for a version that no longer exists, it finds the snapshots the
Internet Archive holds and reads one back through the same fetcher. Ask it for
a year, a month, or a day and it takes the nearest snapshot to that date.

`read_file` reads a file attached to the message. A file over 2000 characters
is named in the prompt rather than pasted into it, because every round resends
the conversation and inline text is paid for on each one; the model reads the
part it needs, and a long file comes back a slice at a time. The tool is only
offered to a run that has a file attached.

`equity` reads a share, index or ETF from Yahoo Finance: price, today's move,
the day and 52-week ranges, volume, market cap, P/E and dividend yield. Several
symbols go in one call. A name is resolved through Yahoo's own search and a
ticker is taken as written, so `IHSG`, `BBCA.JK`, `7203.T` and `^JKSE` all work.
`equity_trend` reads the same listing's candles and reuses the arithmetic the
crypto trend card already had. `screen` ranks one exchange by gainers, losers,
volume or market cap.

Yahoo documents none of that. Its quote and screener endpoints want a cookie
and a crumb, fetched once and re-fetched when a call comes back 401; the chart
endpoint needs neither, which is why a trend reading still answers when the
handshake itself is broken. Prices are delayed by up to fifteen minutes on most
exchanges, and the model is told to say so.

A tool that changes state is refused and never offered to the model. Nothing in
this app can stop and ask whether you meant it, so the answer stays no instead of
becoming yes by default. Same reason a token cannot run one.

## Routes

```
POST   /api/chat                chat, streams SSE
GET    /api/chats               list
GET    /api/chats/{id}          one chat, turns replayed
PATCH  /api/chats/{id}          rename, pin
DELETE /api/chats/{id}
GET    /api/tokens              list
POST   /api/tokens              mint
DELETE /api/tokens/{id}         revoke
POST   /api/auth/register
POST   /api/auth/login
POST   /api/auth/logout
GET    /api/auth/me
POST   /api/auth/profile        name
POST   /api/auth/password
POST   /api/auth/media          avatar, cover, model picture
DELETE /api/auth/media
GET    /api/models              ids, labels, default, capabilities
GET    /api/effort              the think ladder for a model
GET    /api/stats                what the server holds, no tokens spent
POST   /api/prefs               model, effort, ctx, system prompt
GET    /api/health              model server and SearXNG
```

`/api/chat` takes `{messages}`, streams `data:` frames (`delta`, `thinking`,
`tool`, `fetch`, `sources`, `metrics`, `title`, `error`, and one per carded tool:
`weather`, `wayback`, `crypto`, `crypto_trend`, `equity`, `equity_trend`,
`screen`) and ends with `[DONE]`. Each frame carries `r`, the round it came from.

## Front end

Loaded as classic scripts from `static/`, except `settings.js`, which is a
module. Top-level names share one global scope, so `ErisToolCall`,
`ErisMarkdown`, `ErisSidebar`, `ErisPicker`, `ErisStats`, `ErisAttach`,
`ErisLightbox` and `ErisFileView` are each their own IIFE.

Settings is a dialog over the chat page, not a route. It lives at `/#settings`
and opens from the sidebar row. `static/settings.html` is a redirect into that
hash. Esc gives up a pending step first, then closes.

Themes are light, dark, or follow the system, resolved in `theme.js` before first
paint so there's no flash. The choice is in localStorage, because it has to work
on the auth page too.

Tool cards (`static/toolcall.js`) are the collapsed rows you see while the model
works. The header hugs its label, and the panel under it is a card. Thinking uses
the same shell. A search, a page read, a weather reading and an archived page
each get their own card; the weather and wayback ones carry the figures their
tool computed, so they are drawn whole on the frame that ends the call rather
than built up while it runs.

## Tests

```powershell
.venv\Scripts\python.exe tests/run_all.py
```

Seven suites, one total. None of them need Docker or a model.

## Env

`.env.example` has the lot with comments. The ones you'll actually touch:
`MODEL_PATH`, `MODEL_BASE_URL`, `MODEL_NAME`, `SYSTEM_PROMPT`, `SEARXNG_URL`,
`FETCH_PAGES`, `MAX_TOOL_ROUNDS`.