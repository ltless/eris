# Library: Documents

Design spec for the first Library surface. Date: 2026-10-19.

> Amended 2026-10-19 (after review of the built page): the Library ships as a
> **dialog over the chat**, not a route. `static/library.html` is reduced to a
> redirect into `/#library`; the panel itself lives in `static/index.html`
> (`#libModal`), with `library.css` and `library.js` loaded by the chat page.
> There is no back control: Esc, the scrim, and a close button dismiss it. The
> rail below the title carries sections, Documents being the first. Everything
> else here — data model, routes, states, copy — still holds.

## Why

Uploaded documents go into Chroma and then vanish from view. `POST /api/documents/upload`
(`app/api.py:396`) extracts text, chunks it at 800/100, embeds it, and returns a chunk
count. Nothing lists what is in the store, nothing removes it, and a mistaken upload is
permanent. The `doc_search` tool queries a collection the operator cannot inspect.

Library v1 answers two questions: what is indexed, and how do I remove it. Upload moves
into the same page so the store has one home.

## Scope

In: list indexed documents, inspect their chunks, delete a document, upload a document.
Out: per-chat scoping, tags, folders, downloading the original file, and a files-first
store. Each is a later request with its own design.

**Amendment (re-index).** Replacing in place was originally out. It is now in, because the
appending upload was a defect, not a preference: re-uploading a filename left two copies of
every chunk, so retrieval returned the same passage twice and no UI action could undo it.
Upload of an existing name now indexes the new copy first, then retires the previous rows
and their chunks plus any pre-migration chunks with the same `source`
(`rag.retire_previous`), and returns `replaced`. A targeted `where` on user and source is
enough here; the legacy sweep is not needed because only chunks under that filename can
belong to the document being replaced.

## Approach

Chroma alone cannot answer "what is indexed" well. Its chunk metadata carries `source` and
`user_id` (`app/rag.py:51`), so a list built by grouping on `source` has no date, no size,
and no stable identity: two uploads of the same filename merge into one row. Re-uploading
the same file also doubled its chunks, since indexing appended (fixed by the re-index
amendment below).

So a small table records each upload, and each chunk carries its document id:

```
CREATE TABLE documents(
  id TEXT PRIMARY KEY,      -- uuid, also written into chunk metadata as doc_id
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,       -- filename as uploaded
  bytes INTEGER NOT NULL,
  chunks INTEGER NOT NULL,
  created INTEGER NOT NULL
)
```

Each chunk's metadata also gains `n`, its ordinal in the document, so inspection can show
chunks in reading order. Chroma's `get` promises no order, and a chunk list in arbitrary
order is worse than no chunk list.

Chunks indexed before this change have neither key. `list_documents` folds those into one
row per `source`, flagged `legacy`, so existing content stays visible and deletable. A
legacy row has no id in the database, so it is addressed by
`legacy-<sha1(source)[:12]>`, which is URL-safe and recomputable from the source list
during the sweep. Legacy rows report `bytes` and `created` as `null`, and their chunks come
back in Chroma's own order, which is the only order the old chunks have.

The table lives in `library.db` (env `LIBRARY_DB`), owned by a new `app/library.py` that
mirrors the shape of `app/auth.py` and `app/chats.py`: `db_path()`, `db()`, a module-level
`_db`, and `check_same_thread=False`. Chroma stays the only embedding store, and `rag.py`
stays about vectors.

## Verification before coding

`requirements.txt:4` pins nothing for `chromadb`. This design needs `get(where=...)`,
`get(limit=...)`, and `delete(where=...)`. Confirm the installed version supports all
three; if it does not, pin a version that does. The fix is a pin, not a redesign.

## Backend

`app/library.py`

- `add(user_id, doc_id, name, bytes, chunks)` writes one row.
- `list(user_id)` returns rows, newest first.
- `get(user_id, doc_id)` returns one row or None.
- `delete(user_id, doc_id)` removes one row.

`app/rag.py`

- `add_document(user_id, source, text, doc_id=None)` writes `doc_id` and each chunk's `n`
  into its metadata when given.
- `list_documents(user_id)`: table rows first, then one sweep of Chroma for chunks whose
  metadata has no `doc_id`, grouped by `source`. Reads with `user_id` in the `where`
  clause and a limit, since a sweep is the one call that could pull a whole collection.
- `document_chunks(user_id, doc_id, limit)`: chunk text for one document, ordered by `n`,
  capped by the caller.
- `delete_document(user_id, doc_id=None, source=None)`: deletes by `doc_id` when present,
  else by `source` plus `user_id` for legacy rows. The legacy branch is reached by
  recomputing `legacy-<sha1(source)[:12]>` over the user's sources, so no requested id is
  ever trusted as a `where` value on its own.

`app/api.py`

- `POST /api/documents/upload`: unchanged extraction and size limits, adds the uuid, writes
  the row, passes `doc_id` to `add_document`, returns `{id, name, chunks}`.
- `GET /api/documents`: `{items: [{id, name, chunks, bytes, created, legacy}]}`, where a
  legacy row carries `bytes: null` and `created: null`.
- `GET /api/documents/{doc_id}`: the row plus `{n, text}` for the first 50 chunks, so a
  large document does not ship megabytes to the page. The id is a uuid for indexed
  documents and a `legacy-...` id for pre-migration ones.
- `DELETE /api/documents/{doc_id}`: chunks and row, returns `{ok: true}`.

Errors follow the route's existing vocabulary: 413 for over 25MB, 400 for an unreadable or
empty file, 502 when embeddings fail, 503 when Chroma is down, 404 for an unknown or
foreign id. Ownership is enforced on every call; a foreign id is a 404, never a 403.

## Page

New surface: a dialog (`#libModal` in `static/index.html`) with `/library.css` and
`/library.js`, loaded by the chat page. Operate mode, inheriting the Settings panel
pattern, its section rail, and `style.css` tokens. No new visual language.

Composition

- Head: title `Library` and a close button. No back link — nothing navigates.
- Rail: sections, `Documents` first and selected. Room for the later surfaces.
- Panel: the toolbar, the list, the empty state.
- Toolbar: `Index a file` opening a hidden file input (the same pattern the settings
  pictures use), a name filter, and a count line, for example `6 documents · 412 chunks`.
- List: one row per document. Name, then a meta line of `chunks · size · date`. Two
  actions: `Open` expands the row in place to show its chunks, `Delete` starts the
  two-step confirm.
- Legacy rows show `indexed before this panel existed` in place of the date and size.

States

- Empty: `No documents indexed yet.` above the upload action.
- Loading: the list area holds its height, no spinner theatre.
- Upload complete: the row appears with its chunk count.
- Failure: a line inside the row or toolbar carrying the server's message verbatim.
- Delete confirm: the action swaps to `Delete 42 chunks?` with `Cancel` and `Delete`. No
  `confirm()`, which cannot be styled.

Sidebar

One entry below `New chat` and above the `History` title, built from the existing row
anatomy (`.ic` with an authored SVG, plus `.lbl`), so the collapsed rail hides its label
the same way the other rows do. A `button`, not a link: it opens the dialog and carries
`aria-haspopup="dialog"` and `aria-expanded`.

Responsive and input

Single column, rows stack, actions wrap. 44px targets on coarse pointers, matching the
settings work. Below 720px the sheet takes the screen and the rail becomes a chip row.
The filter and the row actions are keyboard reachable, the disclosure carries
`aria-expanded`, Tab cycles inside the sheet while it is open, focus returns to the
sidebar entry on close, and focus rings come from the existing rules.

## Testing

`tests/test_library.py`, the same `check_*` runner as the other files, TestClient with a
temp `LIBRARY_DB` and a monkeypatched `rag.collection()` returning an in-memory stub with
`add`, `get`, and `delete`. Chroma is an HTTP service and the model server is only needed
for embeddings, so neither belongs in the unit checks.

1. Upload writes one row and chunks that carry `doc_id`.
2. List returns rows with chunk counts, newest first.
3. Inspect returns chunk text, capped at 50.
4. Delete removes both the row and the chunks.
5. Chunks without `doc_id` fold into one legacy row, delete by source, and are addressed
   by their recomputed `legacy-...` id.
6. A second user sees nothing and gets 404 on a foreign id.

Smoke test, no browser

`tests/smoke_library.py`, run against a live server. It logs in with a cookie, uploads a
small text file, lists, inspects the returned id, deletes it, and asserts the list no
longer carries it. Legacy rows already in Chroma are reported, not asserted, since they
depend on what the operator indexed earlier.

```
.\.venv\Scripts\python.exe -m uvicorn app.api:app --port 9000
.venv\Scripts\python.exe tests\smoke_library.py --email you@example.com --password ...
```

Needs ChromaDB up and the embedding endpoint reachable, since the smoke path really
embeds. The unit checks do not.

## Risks

- Chroma version differences in `where` syntax, covered by the verification step above.
- A large sweep on list. Bounded by `user_id` and a limit; a single operator's collection
  is small.
- Two documents with one filename are distinguishable in the table but not in legacy rows,
  where they merge. Superseded by the re-index amendment: one filename is now one
  document, so a re-upload retires the other copies instead of keeping them side by side.

## Definition of done

Unit checks pass, the smoke run lists and removes a document end to end, the sidebar entry
reaches the page, and the page renders its empty, populated, inspecting, and confirming
states at desktop and mobile widths.
