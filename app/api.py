"""HTTP surface: routes, validation, and streaming.

This layer holds no execution logic. `/api/chat` builds a RunRequest, drives
the shared engine, and hands its events to `sse`; the engine does the rest.
Who the caller is comes from `deps`, and the model transports from `app/models`,
imported here by the routes directly.
"""

import asyncio
from datetime import date

import httpx
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr

from . import auth, chats, encoding, favicons, tokens
from .config import settings
from .deps import require_session, require_user
from .models import (
    healthy,
    listed_capabilities,
    list_models,
    model_label,
    model_stats,
    title_for,
)
from .models.ladder import cached_reasoning, effort_label, reasoning, saved_effort, supports
from .models.metadata import (
    cached_model_info, cached_properties, cached_window, configured_context,
    llama_window, model_context, model_info, positive_int, requested_context, server_properties,
)
from .runtime.budgets import Budget
from .runtime.context import TRUNCATED, assemble
from .runtime.engine import Engine, RunRequest
from .runtime.routing import select_route
from .security import workspace
from .sse import carded, sse, sse_frame
from .tools import builtin as builtin_tools
from .tools import fetch, search
from .tools.registry import ToolContext, source_view

app = FastAPI(title="Eris")

STATIC_DIR = encoding.STATIC_DIR
MEDIA_DIR = encoding.MEDIA_DIR

builtin_tools.register_builtins()


# Request models.


class AttachedFile(BaseModel):
    name: str = "file"
    text: str

class Message(BaseModel):
    role: str
    content: str | list
    quote: str | None = None
    files: list[AttachedFile] | None = None


class ProfileRequest(BaseModel):
    name: str | None = None


class PasswordRequest(BaseModel):
    old: str
    new: str


class PrefsRequest(BaseModel):
    model: str | None = None
    effort: StrictBool | StrictStr | StrictInt | None = None
    effort_model: str | None = None
    ctx: int | None = Field(default=None, ge=0, le=2**31 - 1)
    system_prompt: str | None = None


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    effort: StrictBool | StrictStr | None = None
    chat_id: str | None = None
    regenerate: bool = False


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


# Chat.


def _last_user_text(messages):
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return next((t for t in map(encoding.text_part_text, c) if t is not None), "")
    return ""


def _with_quote(content, quote):
    """One turn as the model reads it: the quoted words as markdown, then the
    question. The client keeps the two apart so the bubble can show them
    separately; they are joined here, on the way in."""
    quote = (quote or "").strip()
    if not quote:
        return content
    return _prepend_text(content, "\n".join("> " + line for line in quote.splitlines()))


def _prepend_text(content, text):
    """`content` with `text` and a blank line in front of its first words.

    A turn is either a string or a list of parts, and the text belongs in the
    first text part so the images keep their order.
    """
    if isinstance(content, str):
        return f"{text}\n\n{content}" if content else text
    if isinstance(content, list):
        # A part that is not an object carries no text and cannot be given any,
        # so it is dropped rather than handed to `dict()`, which raises on a
        # string, a number, or None and turns the whole request into a 500.
        parts = [dict(p) for p in content if isinstance(p, dict)]
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                p["text"] = f"{text}\n\n{encoding.text_part_text(p)}"
                return parts
        return [{"type": "text", "text": text}, *parts]
    return content


MAX_FILE_CHARS = 40_000
MAX_FILES_CHARS = 60_000
MAX_FILES = 8

# A file longer than this is named in the prompt and read through `read_file`.
# Anything inlined is sent again on every round, so a 40k file that stays
# inline costs its tokens several times over in one turn.
INLINE_FILE_CHARS = 2000

# How long the stream waits for the name it started alongside the run. The
# placeholder the first message wrote is already on screen, so a slow title is
# dropped rather than allowed to hold `[DONE]` and the Stop button with it.
TITLE_WAIT = 20.0


def _clean_files(files):
    """The files of one turn as they will be stored and shown, or `None`.

    The caps are here rather than in the client, because the client is not the
    only caller. A file past the per-file cap is cut with a note the model can
    read, so it knows it is looking at a fragment rather than at the end of the
    file.
    """
    kept = []
    spent = 0
    for f in files or []:
        text = f.text
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS].rstrip() + TRUNCATED
        if spent + len(text) > MAX_FILES_CHARS:
            break
        name = (f.name or "file").strip()[:120] or "file"
        kept.append({"name": name, "text": text})
        spent += len(text)
        if len(kept) >= MAX_FILES:
            break
    return kept or None


def _with_files(content, files):
    """The turn as the model reads it: each file under its own name, in a fence
    that keeps its code out of the markdown, then the question. The client keeps
    them apart so a long file does not fill the bubble.

    A long file is named, not pasted: its text is read on demand through
    `read_file`, which is also what a follow-up round can go back to.
    """
    if not files:
        return content
    blocks = []
    for f in files:
        if len(f["text"]) > INLINE_FILE_CHARS:
            blocks.append(
                f"### File: {f['name']} ({len(f['text'])} characters, not inlined)\n"
                f"Call read_file with name={f['name']!r} to read it."
            )
            continue
        lang = f["name"].rsplit(".", 1)[-1].lower() if "." in f["name"] else ""
        # Only a plain word goes in the fence's info string; a name with a space
        # in it would end it.
        lang = lang if lang.isalnum() and len(lang) <= 12 else ""
        blocks.append(f"### File: {f['name']}\n```{lang}\n{f['text']}\n```")
    return _prepend_text(content, "\n\n".join(blocks))


def _last_user_files(messages, cleaned):
    """What was attached to the question being asked, in the order it was
    attached. Only the newest user turn: older files come back from storage
    when the chat is reopened."""
    for m, files in zip(reversed(messages), reversed(cleaned)):
        if m.role == "user":
            return files
    return None


def _run_files(messages, cleaned):
    """Every file this run may read, oldest turn first, so `read_file` can find
    any name the prompt shows. The prompt names a file from an earlier turn as
    well, and a tool that answers "no such attachment" to a name it was just
    given would be worse than not offering the tool at all. A name used twice
    resolves to the newer one, which is what the model reading the prompt sees
    last."""
    by_name = {}
    for m, files in zip(messages, cleaned):
        if m.role != "user":
            continue
        for f in files or ():
            by_name[f["name"]] = f
    return tuple(by_name.values())


def _last_user_images(messages):
    """What the reader attached to the question being asked.

    The bytes are taken from the same content parts the model was given, so the
    turn that is stored is the turn that was run. Only the newest user message
    is read: an image belongs to the turn it was attached to, and the older ones
    come back from storage when the chat is reopened.
    """
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            return []
        raws = [chats.decode_data_url(encoding.image_part_url(p)) for p in content]
        return [raw for raw in raws if raw]
    return []


def _prefs(u):
    """The account's stored settings. A request names a model only when it
    overrides one, and the stored pick is the one every route falls back to."""
    return u.get("prefs") or {}


def _asking_model(asked, prefs, fallback=None):
    """The model a request names first, then the account's stored pick, then
    whatever the caller answers for neither."""
    return asked or prefs.get("model") or fallback


def _request_model(asked, u):
    """The model this run will use, from the request, then the account, then
    the configured one.

    One resolver for every caller that has to name it: `/api/chat` stores the
    answer on the run, and a caller that resolved it with a different fallback
    would report one model while the server ran another.
    """
    return _asking_model(asked, _prefs(u), settings.model_name)


def _wanted_ctx(prefs):
    """The context the account asked for in the panel, or `None` for its
    default. Stored as 0 when unset, which is not a context to ask for."""
    return positive_int(prefs.get("ctx"))


def _saved_effort(prefs, model, profile):
    legacy_model = prefs.get("effort_legacy_model") or _asking_model(None, prefs, settings.model_name)
    return saved_effort(prefs, model, legacy_model, profile)


def _build_request(req: ChatRequest, u, messages, files, workspace_access=None, routing=None):
    prefs = _prefs(u)
    base_prompt = (prefs.get("system_prompt") or "").strip() or settings.system_prompt
    system = assemble(
        base_prompt=base_prompt,
        today=date.today().isoformat(),
        instruction=(
            "Use relevant tool results for current facts. If the data could not be "
            "verified, say so. Ask a short question if a required detail is missing. "
            "Use the plan tool, when offered, for tasks with dependent steps."
        ),
    )
    tool_ctx = ToolContext(files=tuple(files), workspace=workspace_access)
    model = _request_model(req.model, u)
    profile = cached_reasoning(model)
    thinking = _saved_effort(prefs, model, profile)
    if "effort" in req.model_fields_set:
        thinking = req.effort if supports(profile, req.effort) else None
    shown = cached_model_info(model)
    fixed = llama_window(cached_properties(model))
    maximum = model_context(shown)
    context = None if fixed else requested_context(_wanted_ctx(prefs), maximum)
    window = fixed or context or configured_context(shown) or cached_window(model)
    # A reopened chat sends its stored turns back with each question; the engine
    # windows them, so what arrives here is everything the chat ever held.
    return RunRequest(
        messages=[{"role": "system", "content": system}] + messages,
        model=model,
        thinking=thinking,
        ctx_tokens=context,
        window_tokens=requested_context(window, maximum),
        budget=Budget(),
        tool_context=tool_ctx,
        routing=routing,
    )


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request, u: dict = Depends(require_user)):
    # The two readers below look only at role and content, and both want plain
    # dicts rather than the request models, so the projection is made once.
    plain = [{"role": m.role, "content": m.content} for m in req.messages]
    # The question is what was typed, the quote is what it answers. They are
    # stored apart so a reload can draw them as two things, and joined only for
    # the model, which reads a quoted line as the reader's own words.
    query = _last_user_text(plain)
    latest_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
    quoted = ((latest_user.quote or "").strip() or None) if latest_user else None
    # Capped once: the same cleaned turn is what the model reads, what the run
    # may hand to `read_file`, and what the database stores.
    cleaned = [_clean_files(m.files) for m in req.messages]
    # A reply can be nothing but a selection, with no words typed under it. The
    # quote is what was said, then, and it names the chat when the question is
    # empty.
    # A file alone is a question too, so it can name the chat like a quote can.
    files = _last_user_files(req.messages, cleaned)
    subject = query or quoted or (files[0]["name"] if files else "")
    messages = [
        {"role": m.role, "content": _with_quote(_with_files(m.content, f), m.quote)}
        for m, f in zip(req.messages, cleaned)
    ]
    attached = _last_user_images(plain)
    access = workspace.for_user(u["id"]) if getattr(request.state, "session_auth", False) else None
    routing = None
    if settings.dynamic_tools:
        user_turns = [m for m in plain if m["role"] == "user"]
        routing = select_route(query, previous=_last_user_text(user_turns[:-1]),
                               has_attachments=bool(files))
    model = _request_model(req.model, u)
    prefs = _prefs(u)
    if req.effort is not None or prefs.get("efforts") or "effort" in prefs or _wanted_ctx(prefs):
        profile = await reasoning(model)
        if req.effort is not None and profile.source != "unavailable" and not supports(profile, req.effort):
            raise HTTPException(422, "That effort is not supported by this model.")
    run_request = _build_request(req, u, messages, _run_files(req.messages, cleaned), access, routing)
    engine = Engine(run_request)

    chat_run = None
    name_this = False
    if req.chat_id and req.regenerate:
        chat_run = chats.start_regeneration(u["id"], req.chat_id)
        if chat_run is None:
            raise HTTPException(409, "There is no current answer to regenerate.")
    elif req.chat_id and (query.strip() or quoted or attached or files):
        name_this = bool(subject and chats.untitled(req.chat_id, u["id"]))
        chat_run = chats.save_turn(
            u["id"], req.chat_id, "user", query, quote=quoted,
            images=attached or None, files=files,
        )
        if chat_run is None:
            raise HTTPException(404, "Chat not found.")
        chats.touch_title(req.chat_id, subject, u["id"], instance=chat_run.instance)

    async def gen():
        title_task = (
            asyncio.create_task(title_for(subject, model=run_request.model, ctx=run_request.ctx_tokens))
            if name_this else None
        )
        web_groups = []
        fetch_rows = []
        card_rows = []
        answer_rounds = {}
        thinking_rounds = {}
        completed = False
        saved = None

        def remember():
            if not chat_run or (chat_run.regenerate and not completed):
                return None
            answers = [{"r": r, "text": "".join(parts)} for r, parts in answer_rounds.items()]
            answer = "\n\n".join(row["text"] for row in answers)
            if not answer.strip():
                return None
            thinking = ["".join(thinking_rounds.get(r, ()))
                        for r in range(max(thinking_rounds, default=-1) + 1)]
            return chats.finish_run(
                chat_run, answer,
                thinking=thinking or None,
                sources=_stored_sources(engine.result.sources),
                web=[g for g in web_groups if g["items"] or not g.get("ok", True)],
                fetches=fetch_rows or None,
                cards=card_rows or None,
                answers=answers if len(answers) > 1 else None,
            )

        stream = engine.run()
        try:
            async for event in stream:
                frame = sse_frame(event)
                if frame is None:
                    continue
                # Branched on the frame's own type, never on the event's: which
                # event became which frame is stated in `sse_frame` and nowhere
                # else, so asking it a second time here would be a second copy
                # of that mapping. A carded tool's frame is named after the tool,
                # so `carded()` is what tells a card apart from any other frame.
                kind = frame["type"]
                if kind == "delta":
                    answer_rounds.setdefault(frame["r"], []).append(frame["text"])
                elif kind == "thinking":
                    thinking_rounds.setdefault(frame["r"], []).append(frame["text"])
                elif kind == "tool":
                    web_groups.append({"r": frame["r"], "id": frame.get("id", ""),
                                       "query": frame["query"], "items": [],
                                       "ok": True, "reason": ""})
                elif kind == "fetch":
                    # Recorded from the start frame, not the closing one: a read
                    # that failed is still a read the user watched, and the URL
                    # is only ever stated here.
                    fetch_rows.append({"r": frame["r"], "id": frame.get("id", ""),
                                       "url": frame["url"], "chars": 0, "ok": True,
                                       "reason": ""})
                elif kind == "fetch_done":
                    row = _fetch_row(fetch_rows, frame, opened=True)
                    if row is not None:
                        row["chars"] = frame["chars"]
                elif kind == "tool_error" and frame.get("tool") == fetch.NAME:
                    row = _fetch_row(fetch_rows, frame, opened=False)
                    if row is not None:
                        row["ok"] = False
                        # Stored so the card says why after a reload; the
                        # streamed frame is the only other place it exists.
                        row["reason"] = frame.get("reason", "")
                elif kind == "tool_error" and frame.get("tool") == search.NAME:
                    # A search that failed drew its card and said why, and the
                    # streamed frame is the only place that sentence exists. A
                    # reload reads the stored turn, so without this the card is
                    # simply gone and the turn reads as if nothing was asked.
                    group = next((g for g in web_groups
                                  if g["r"] == frame["r"]
                                  and g["id"] == frame.get("id", "")), None)
                    if group is not None:
                        group["ok"] = False
                        group["reason"] = frame.get("reason", "")
                elif kind == "tool_error" and frame.get("tool") in carded():
                    # A card still appears, in its failed state, so a reload
                    # shows the failure rather than a gap where a call was.
                    card_rows.append({"r": frame["r"], "tool": frame["tool"],
                                      "card": None, "reason": frame.get("reason", "")})
                elif kind in carded():
                    # One row per card, in the order they were drawn, each
                    # carrying the round it belongs to so a replay puts it back
                    # among that round's other cards.
                    card_rows.append({"r": frame["r"], "tool": kind, "card": frame["card"]})
                elif kind == "sources":
                    items = [i for i in frame["items"] if i.get("kind") == "web"]
                    if items:
                        # A group is opened by a search's own `tool` frame and
                        # filed into by the rows that carry its id. Every tool
                        # that reports a source labels it `web`, so a crypto or
                        # weather row in the same round used to be appended to
                        # the search group and came back as a result of a search
                        # nobody ran. No group, no filing: it is still a
                        # citation, and still forwarded below.
                        group = next((g for g in web_groups
                                      if g["r"] == frame["r"]
                                      and g["id"] == frame.get("id", "")), None)
                        if group is not None:
                            group["items"].extend(items)
                        yield sse({"type": "sources", "r": frame["r"],
                                   "id": frame.get("id", ""), "items": items})
                    # The unfiltered frame would repeat rows that were dropped
                    # as non-web, so this frame is the one that is not forwarded.
                    continue
                yield sse(frame)
            completed = engine.result.stop_reason == "answered"
        finally:
            try:
                saved = remember()
            finally:
                if title_task is not None and (not completed or saved is False):
                    title_task.cancel()
                await stream.aclose()
        if saved is False:
            yield sse({"type": "error", "code": "chat_changed",
                       "message": "This chat changed before the answer could be saved."})
        if title_task is not None and completed and saved is not False:
            try:
                title = await asyncio.wait_for(title_task, TITLE_WAIT)
            except Exception:
                # A name that timed out or errored is the failure case, and the
                # opening words the first message wrote are already on screen.
                title = ""
            if title:
                # Dropped when the stored name is no longer the opening words:
                # a name someone chose, typed while the answer streamed, wins.
                if chats.name_from_model(u["id"], req.chat_id, title, subject,
                                         instance=chat_run.instance):
                    yield sse({"type": "title", "chat_id": req.chat_id, "title": title})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _fetch_row(rows, frame, *, opened):
    """The stored fetch row a closing frame belongs to, or `None`.

    Matched by the call's own id first. A round runs its calls together and
    reports them as they finish, so two reads in one round arrive in either
    order; the round alone would close whichever row happened to be first, and
    a stored turn would then say the wrong page was the one that failed.

    `opened` says which side the frame is: a `fetch_done` closes a row that has
    no size yet, a `tool_error` closes one that has not failed yet. Older turns
    and servers that name no id fall back to the round, which is what the
    previous version matched on and is still right whenever a round holds one
    read.
    """
    call = frame.get("id", "")
    for row in reversed(rows):
        if row["r"] != frame["r"]:
            continue
        if call and row.get("id") != call:
            continue
        # A row that already carries a size or already failed is a row some
        # earlier frame closed, so it is not this one's.
        if (row["chars"] == 0) if opened else row["ok"]:
            return row
    return None


def _stored_sources(sources):
    """The `sources` rows a turn keeps. The run already numbered each one as it
    found it, so the stored `n` is the one the live frames carried: renumbering
    here would move every citation of a turn with more than one search. The
    fields are `source_view`'s, so a rewound turn and a live one draw the same
    row."""
    return [{"n": s["n"], **source_view(s)} for s in sources]


# Stored chats.


@app.get("/api/chats")
async def list_chats(u: dict = Depends(require_user)):
    return {"items": chats.list_chats(u["id"])}


@app.get("/api/chats/{chat_id}")
async def chat_history(chat_id: str, u: dict = Depends(require_user)):
    return {"items": chats.turns(u["id"], chat_id)}


class ChatPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None


@app.patch("/api/chats/{chat_id}")
async def chat_patch(chat_id: str, req: ChatPatch, u: dict = Depends(require_session)):
    if req.title is None and req.pinned is None:
        raise HTTPException(400, "nothing to update")
    chats.set_meta(u["id"], chat_id, req.title, req.pinned)
    return {"ok": True}


@app.delete("/api/chats/{chat_id}")
async def chat_delete(chat_id: str, u: dict = Depends(require_session)):
    chats.delete(u["id"], chat_id)
    return {"ok": True}


# API tokens.


class TokenNew(BaseModel):
    name: str = ""


@app.get("/api/tokens")
async def list_tokens(u: dict = Depends(require_session)):
    """The account's tokens, with the plaintext of none of them.

    Only the prefix comes back: a list of rows that all read `eris_` is a list
    the reader cannot tell apart, and a token that could be read back would be a
    credential sitting in a page cache.
    """
    return {"items": tokens.list_all(u["id"])}


@app.post("/api/tokens")
async def add_token(req: TokenNew, u: dict = Depends(require_session)):
    item = tokens.create(u["id"], req.name)
    if not item:
        raise HTTPException(400, f"too many tokens ({tokens.MAX_TOKENS})")
    return item


@app.delete("/api/tokens/{token_id}")
async def revoke_token(token_id: str, u: dict = Depends(require_session)):
    if not tokens.revoke(u["id"], token_id):
        raise HTTPException(404, "unknown token")
    return {"ok": True}


# Auth.


def _set_session(res, token):
    """Hand the browser its session cookie. One place states how a session is
    carried, so a flag set here cannot be missing from one of the three routes
    that sign someone in."""
    res.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                   max_age=auth.SESSION_TTL, path="/")


@app.post("/api/auth/register")
async def register(req: RegisterRequest, res: Response):
    u, err = auth.create_user(req.name, req.email, req.password)
    if err:
        raise HTTPException(400, err)
    _set_session(res, auth.new_session(u["id"]))
    return u


@app.post("/api/auth/login")
async def login(req: LoginRequest, res: Response):
    u = auth.verify_user(req.email, req.password)
    if not u:
        raise HTTPException(401, "email/password salah")
    _set_session(res, auth.new_session(u["id"]))
    return u


@app.post("/api/auth/profile")
async def update_profile(req: ProfileRequest, u: dict = Depends(require_session)):
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "name required")
        auth.set_name(u["id"], name[:40])
    return auth.profile(auth.user_row(u["id"]))


@app.post("/api/auth/password")
async def update_password(req: PasswordRequest, res: Response, u: dict = Depends(require_session)):
    token, err = auth.change_password(u["id"], req.old, req.new)
    if err:
        raise HTTPException(400, err)
    _set_session(res, token)
    return {"ok": True}


@app.get("/api/models")
async def models(u: dict = Depends(require_user)):
    # `return_exceptions` because the picker needs both, and one of them
    # failing should cost the other: a listing that throws would otherwise take
    # the model ids down with it, and the ids are what the composer cannot do
    # without. `list_models` answers `[]` for a server it cannot reach, which is
    # the same shape this hand-back produces.
    items, caps = await asyncio.gather(
        list_models(), listed_capabilities(), return_exceptions=True)
    items = items if isinstance(items, list) else []
    caps = caps if isinstance(caps, dict) else {}
    prefs = _prefs(u)
    default = _asking_model(None, prefs, settings.model_name)
    if default not in items:
        items.insert(0, default)
    # The id is what the endpoint accepts and what prefs store; the label is
    # what a person reads. Parallel rather than a list of objects, so a client
    # that only knows `items` keeps working unchanged.
    #
    # `capabilities` is read here because this is the one route the composer
    # already calls: the answer also fills the cache the chat loop reads, so a
    # chat sent without opening a menu still knows whether to offer tools. A
    # model the endpoint says nothing about is absent from the map, and the
    # client reads a missing entry the same way the engine does — unknown, so
    # nothing is hidden.
    return {
        "items": items,
        "labels": {mid: model_label(mid) for mid in items},
        "default": default,
        "capabilities": {mid: caps[mid] for mid in items if mid in caps},
    }


@app.get("/api/effort")
async def effort(model: str | None = None, u: dict = Depends(require_user)):
    """The selected model's declared controls and the account's preference."""
    prefs = _prefs(u)
    mid = _asking_model(model, prefs, settings.model_name)
    profile = await reasoning(mid)
    choices = []
    if len(profile.values) > 1:
        default = effort_label(profile.default) if profile.default is not None else None
        choices = [{"value": None, "label": f"Default ({default})" if default else "Default"}]
        choices.extend({"value": value, "label": effort_label(value)} for value in profile.values)
    return {
        "model": mid,
        "efforts": choices,
        "current": _saved_effort(prefs, mid, profile),
        "default": profile.default,
        "source": profile.source,
    }


@app.get("/api/stats")
async def server_stats(model: str | None = None, u: dict = Depends(require_user)):
    """What the model server is holding, for the panel in the composer.

    Nothing is generated, so this can be polled mid-chat without spending a
    token. `ctx` is passed because the KV figure is sized against it when the
    model is not loaded yet.
    """
    prefs = _prefs(u)
    mid = _asking_model(model, prefs, settings.model_name)
    return await model_stats(mid, ctx=_wanted_ctx(prefs))


@app.post("/api/prefs")
async def update_prefs(req: PrefsRequest, u: dict = Depends(require_session)):
    stored = _prefs(u)
    selected = (_request_model(None, u) if req.model is None
                else req.model.strip()[:200] or settings.model_name)
    effort_model = (req.effort_model or selected).strip()[:200] or selected
    value = req.effort
    if value is not None:
        profile = await reasoning(effort_model)
        if type(value) is int and 0 <= value < len(profile.values):
            value = profile.values[value]
        if not supports(profile, value):
            raise HTTPException(422, "That effort is not supported by this model.")
    ctx = req.ctx
    if ctx:
        shown = await model_info(selected)
        props = await server_properties(selected) if shown is None else None
        if llama_window(props):
            raise HTTPException(422, "This server sets its context at startup.")
        ctx = requested_context(ctx, model_context(shown))

    def change(prefs):
        if type(prefs.get("effort")) is int and "effort_legacy_model" not in prefs:
            prefs["effort_legacy_model"] = _asking_model(None, prefs, settings.model_name)
        if req.model is not None:
            # A Hugging Face GGUF id runs past 80 characters, and a cut one is an
            # id the server does not have: 404 on the next question.
            model = req.model.strip()[:200]
            # An empty id is the one value that cannot mean "use the configured
            # model": it would sit in prefs ahead of that fallback and every run
            # would ask the server for a model named "". Clearing the key is what
            # forgetting a choice looks like; storing nothing is refused.
            if model:
                prefs["model"] = model
            else:
                prefs.pop("model", None)
        if "effort" in req.model_fields_set:
            efforts = dict(prefs["efforts"]) if isinstance(prefs.get("efforts"), dict) else {}
            efforts[effort_model] = value
            prefs["efforts"] = efforts
        if ctx is not None:
            prefs["ctx"] = ctx
        if req.system_prompt is not None:
            # Empty string clears the override; chat falls back to the env prompt.
            prompt = req.system_prompt.strip()
            if prompt:
                prefs["system_prompt"] = prompt[:4000]
            else:
                prefs.pop("system_prompt", None)

    # The blob is shared with the media upload, so it is read and written as one
    # step rather than from a copy this request took earlier.
    prefs = auth.update_prefs(u["id"], change)
    # The answer states the model the account is on now, resolved the same way
    # a run resolves it. A client that only kept the id it wrote would show a
    # pick that a later fallback had already moved away from.
    return {"ok": True, "model": _request_model(None, {**u, "prefs": prefs})}


# The kinds an upload may name, read off the one place that says where each one
# is kept, so a slot added there is accepted here without a second list.
MEDIA_KINDS = tuple(auth.MEDIA_SLOTS)


def _swap_media(u: dict, kind: str, url: str | None):
    """Point one of the account's pictures at `url`, deleting the file it
    replaced. Only a stored `/uploads/` path is removed: anything else was not
    put there by this route."""
    old = auth.media_url(u, kind)
    auth.set_media(u["id"], kind, url)
    if old and old.startswith("/uploads/"):
        (MEDIA_DIR / old.split("/")[-1]).unlink(missing_ok=True)


@app.post("/api/auth/media")
async def upload_media(file: UploadFile, kind: str = "avatar", u: dict = Depends(require_session)):
    if kind not in MEDIA_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(MEDIA_KINDS)}")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "image required")
    # Read in chunks so the size is refused while it is still small. Reading
    # the whole upload first would buffer however much a client sent into
    # memory before the check could reject it.
    MAX_UPLOAD = 2_000_000
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(400, "image too large (max 2MB)")
    ext = encoding.image_extension(file.content_type)
    name = f"{u['id']}-{kind}-{encoding.new_token()}.{ext}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(raw)
    _swap_media(u, kind, f"/uploads/{name}")
    return {"url": f"/uploads/{name}"}


@app.delete("/api/auth/media")
async def delete_media(kind: str = "avatar", u: dict = Depends(require_session)):
    if kind not in MEDIA_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(MEDIA_KINDS)}")
    _swap_media(u, kind, None)
    return {"ok": True}


@app.post("/api/auth/logout")
async def logout(session: str | None = Cookie(None, alias=auth.SESSION_COOKIE)):
    auth.drop_session(session)
    r = RedirectResponse("/", 303)
    r.delete_cookie(auth.SESSION_COOKIE, path="/")
    return r


@app.get("/api/auth/me")
async def me(u: dict = Depends(require_user)):
    """Who this call is, by cookie or by token."""
    return u


@app.get("/api/favicon")
async def favicon(domain: str, u: dict = Depends(require_user)):
    """A result row's site icon, proxied from the site so no third party sees
    which results were shown. A site without one is a 404, and the row keeps
    its letter chip."""
    icon = await favicons.icon_for(domain)
    if not icon:
        raise HTTPException(404, "no icon")
    raw, ctype = icon
    return Response(raw, media_type=ctype, headers={"Cache-Control": favicons.CACHE_CONTROL})


# Health.


@app.get("/api/health")
async def health():
    """Each dependency the chat needs, as up or down."""
    out = {"model": "up" if await healthy() else "down"}
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.searxng_url}/healthz")
            out["search"] = "up" if r.status_code < 400 else "down"
    except Exception:
        out["search"] = "down"
    return out


# Pages.


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not auth.get_session(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse("/auth.html", 303)
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
