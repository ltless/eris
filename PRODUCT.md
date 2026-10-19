# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One operator: the person who owns and runs the machine Eris is deployed on. They use
it on their own hardware, on their own network, for their own work. There is no
public audience, no external customer, and no support relationship. The user and
the maintainer are the same person.

The user's job is to get reliable answers out of a local model without sending
anything off the machine: ask a question, let Eris search and read sources, and end
up with a cited answer they can act on or keep.

## Product Purpose

Eris is a minimal, self-hosted chat UI over a local llama.cpp model server, with
retrieval over local documents and live web search. It exists so a single operator
can run a capable assistant entirely on hardware they control. Success means the
operator reaches for Eris instead of a hosted chat product for research-shaped
questions, and trusts the answer because the sources are visible and clickable.

## Positioning

Deep research that actually cites: multi-round tool use that searches the web, fetches
and reads pages, then returns an answer whose claims link back to the pages they came
from. The whole loop runs locally over a model the operator owns.

## Operating Context

- Local stack: FastAPI + uvicorn app (`app/api.py`) on port 9000, serving a static
  frontend; llama.cpp `llama-server` (or Ollama) streaming chat from the native
  `/api/chat` NDJSON endpoint and embeddings from `/v1/embeddings`; ChromaDB in
  Docker for retrieval; SearXNG in Docker for web search.
- Configuration lives in repo-root `.env` (`app/config.py`), including model endpoint,
  Chroma host/port, SearXNG settings, top-k values, tool-round limits, and the system
  prompt.
- Startup is scripted: `start.sh` on POSIX, `start.ps1` on Windows, plus
  `run-model.sh` / `run-model.ps1` for the model server alone. Windows and Linux are
  both first-class; the operator may be on either.
- The browser client is the whole interface: a chat page (`static/index.html`) that
  hosts the Library and Settings dialogs, and an auth page (`static/auth.html`), all
  vanilla HTML/CSS/JS with no build step. Streaming arrives as SSE over a POST to
  `/api/chat`.
- Chat history, accounts, and sessions are local SQLite files (`chats.db`, `users.db`).
- The operator runs the app themselves and interacts with it over `localhost` in a
  desktop browser; there is no hosted deployment.

## Capabilities and Constraints

Confirmed functionality (from the implementation):

- Streaming chat with markdown rendering (`marked.js`), sanitization (`DOMPurify`),
  and code highlighting (`highlight.js`); visible thinking and tool-call rows.
- Retrieval-augmented answers over uploaded documents: upload endpoint, ~800-char
  chunks with 100-char overlap stored in ChromaDB with a `source` field, a
  `doc_id`, and the chunk's ordinal, top-`RAG_TOP_K` chunks retrieved. PDF and
  text extraction supported. The Library panel, a dialog over the chat reached
  from the sidebar, lists the indexed documents, opens one document's chunks,
  deletes a document with its chunks, and takes an upload. Uploading a filename
  that is already indexed replaces it rather than appending a second copy.
- Web search via SearXNG with an optional page-fetch step, and a tool loop with a
  normal round budget and a larger deep-research budget.
- Two interaction modes exposed in the UI: default and Deep (deep research).
- Accounts with register/login/logout/session cookies, profile and password editing,
  avatar, cover, and model picture upload, per-user preferences, and per-chat history
  with rename/delete.
- Settings dialog for model choice, the model picture, and security actions; health
  polling from the sidebar.

Constraints and open decisions:

- **The stack is not binding.** The operator placed no constraint on frontend or
  backend technology; the current vanilla HTML/CSS/JS + FastAPI implementation is the
  incumbent, not a mandate. Future work may change it where the work calls for it.
- Authentication is incidental to the product's purpose: it exists as a lock on the
  door for a single-operator install, not as a multi-user feature. The code's own note
  (`app/auth.py`) marks multi-instance/multi-user scale as out of scope until proven
  otherwise. Whether auth stays is an undecided product fact.
- No decision recorded on packaging, distribution, or onboarding for the app beyond
  the existing start scripts.
- Local hardware is the ceiling on model quality and speed; the UI must not promise
  more than the operator's machine can deliver.

## Brand Commitments

The name **Eris** only. No logo, palette, typography, tone-of-voice, or personality
commitment exists, so visual and verbal identity are unbound and may be chosen freely.

## Evidence on Hand

Inspected: the working application, its README, configuration, and start scripts.
Absent and not to be fabricated: user testimonials, customer logos, benchmarks,
press coverage, adoption numbers, pricing, or any claim about real-world usage by
anyone other than the operator.

## Product Principles

1. **The source is part of the answer.** An answer without visible, followable
   citations is an unfinished answer, not a shorter one.
2. **Local by default.** Nothing about the operator's questions, documents, or history
   leaves their machine unless they explicitly asked for a web search.
3. **One operator, fully in control.** Configuration, model, data, and failure modes
   stay legible to the person running it; no hidden service dependency.
4. **The interface stays out of the way.** Reading the answer is the task; chrome,
   ceremony, and setup friction are costs to be minimized.
5. **Tools are visible, not magical.** The operator can see what Eris searched, read,
   and reasoned through, and can tell when it is working versus when it is stuck.