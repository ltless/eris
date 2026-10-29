"""HTTP surface: routes, validation, and streaming.

This layer holds no execution logic. `/api/chat` builds a RunRequest, drives
the shared engine, and hands its events to `sse`; the engine does the rest.
Who the caller is comes from `deps`, the MCP endpoint from `mcp`, and the model
transports from `app/models`, re-exported here because the routes and the
existing tests still refer to them by these names.
"""

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
from pydantic import BaseModel

from . import auth, chats, encoding, mcp, tokens
from .config import settings
from .deps import db_user, require_session, require_user
from .models import (
    FALLBACK_LADDER,
    ModelError,
    cached_ladder,
    effort_label,
    healthy,
    listed_capabilities,
    list_models,
    model_label,
    model_stats,
    rung_index,
    think_ladder,
    think_value,
    # Re-exported for the tests, which reach the transport through this module.
    stream_completion,
    to_native,
)
from .runtime import events as ev
from .runtime.budgets import Budget
from .runtime.context import ContextBudget, assemble
from .runtime.engine import Engine, RunRequest
from .runtime.events import Emitter
from .sse import sse, sse_frame
from .tools import builtin as builtin_tools
from .tools.registry import ToolContext

app = FastAPI(title="Eris")

ROOT = encoding.ROOT
STATIC_DIR = encoding.STATIC_DIR
MEDIA_DIR = encoding.MEDIA_DIR

builtin_tools.register_builtins()
app.include_router(mcp.router)


# --- request models ---


class Message(BaseModel):
    role: str
    content: str | list


class ProfileRequest(BaseModel):
    name: str | None = None


class PasswordRequest(BaseModel):
    old: str
    new: str


class PrefsRequest(BaseModel):
    model: str | None = None
    effort: int | None = None
    ctx: int | None = None
    system_prompt: str | None = None


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    chat_id: str | None = None
    regenerate: bool = False


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


# --- chat ---


def _last_user_text(messages):
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return next(
                (p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"),
                "",
            )
    return ""


def _build_request(req: ChatRequest, u, messages):
    prefs = u.get("prefs") or {}
    base_prompt = (prefs.get("system_prompt") or "").strip() or settings.system_prompt
    system = assemble(
        ContextBudget(),
        base_prompt=base_prompt,
        today=date.today().isoformat(),
        instruction=(
            "For anything current or recent, answer only from the search results you "
            "receive, never from training memory."
        ),
    )
    tool_ctx = ToolContext(
        user_id=u["id"],
        approved=set(prefs.get("approved") or ()),
    )
    model = req.model or prefs.get("model") or None
    # The stored effort is an index, and which value it means belongs to the
    # model being asked: a ladder is per model, so it is resolved here, against
    # this model's own, rather than sent as the index it was remembered as.
    #
    # The ladder is read from the cache and never asked for here. A probe on a
    # model that is not loaded costs the load, and a chat must not wait on the
    # panel's question; when the panel has not opened yet, `think_value` answers
    # for the unknown case with the value the request carried before any ladder.
    ladder = cached_ladder(model)
    return RunRequest(
        messages=[{"role": "system", "content": system}] + messages,
        model=model,
        thinking=think_value(prefs.get("effort"), ladder),
        ctx_tokens=int(prefs.get("ctx") or 0) or None,
        tags=("chat",),
        budget=Budget(rounds=settings.max_tool_rounds, extra_rounds=settings.extra_tool_rounds),
        tool_context=tool_ctx,
        system_prompt=base_prompt,
    )


@app.post("/api/chat")
async def chat(req: ChatRequest, u: dict = Depends(require_user)):
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    query = _last_user_text(messages)
    request = _build_request(req, u, messages)
    engine = Engine(request, emitter=Emitter())

    async def gen():
        if req.regenerate:
            chats.drop_last_assistant(u["id"], req.chat_id)
        web_groups = []
        fetch_rows = []
        try:
            async for event in engine.run():
                frame = sse_frame(event)
                if frame is not None:
                    if event.type == ev.TOOL_STARTED and frame.get("type") == "tool":
                        web_groups.append({"r": frame["r"], "query": frame["query"], "items": []})
                    elif event.type == ev.TOOL_STARTED and frame.get("type") == "fetch":
                        # Recorded from the start frame, not the closing one: a
                        # read that failed is still a read the user watched, and
                        # the URL is only ever stated here.
                        fetch_rows.append({"r": frame["r"], "url": frame["url"],
                                           "chars": 0, "ok": True})
                    elif event.type == ev.TOOL_COMPLETED and frame.get("type") == "fetch_done":
                        row = next((f for f in reversed(fetch_rows)
                                    if f["r"] == frame["r"] and not f["chars"]), None)
                        if row is not None:
                            row["chars"] = frame["chars"]
                    elif event.type == ev.TOOL_FAILED and frame.get("tool") == "fetch_url":
                        row = next((f for f in reversed(fetch_rows)
                                    if f["r"] == frame["r"] and f["ok"]), None)
                        if row is not None:
                            row["ok"] = False
                    elif event.type == ev.SOURCE_FOUND and frame.get("type") == "sources":
                        items = [i for i in frame["items"] if i.get("kind") == "web"]
                        if items:
                            group = next((g for g in web_groups if g["r"] == frame["r"]), None)
                            if group is None:
                                group = {"r": frame["r"], "query": "", "items": []}
                                web_groups.append(group)
                            group["items"].extend(items)
                            yield sse({"type": "sources", "r": frame["r"], "items": items})
                    if frame.get("type") != "sources":
                        yield sse(frame)
        except ModelError as e:
            yield sse({"type": "error", "message": f"Cannot reach model server: {e}"})
        else:
            yield "data: [DONE]\n\n"
        finally:
            # Persist even if the client hit Stop mid-stream: the turn happened
            # and should still be there after a refresh. No yields are allowed
            # in here, which is why the sources are collected above instead.
            if req.chat_id:
                if query and not req.regenerate:
                    chats.save_turn(u["id"], req.chat_id, "user", query)
                    chats.touch_title(req.chat_id, query)
                answer = engine.result.answer
                if answer:
                    chats.save_turn(
                        u["id"], req.chat_id, "assistant", answer,
                        thinking=engine.result.thinking or None,
                        sources=_number_sources(engine.sources),
                        web=[g for g in web_groups if g["items"]],
                        fetches=fetch_rows or None,
                    )

    return StreamingResponse(gen(), media_type="text/event-stream")


def _number_sources(sources):
    out = []
    for i, s in enumerate(sources):
        out.append({"n": i + 1, "source": s.get("source", ""), "kind": s.get("kind", "web"),
                    **({"title": s["title"]} if s.get("title") else {})})
    return out


# --- chats ---


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


# --- tokens ---


class TokenNew(BaseModel):
    name: str = ""


@app.get("/api/tokens")
async def list_tokens(u: dict = Depends(require_session)):
    """The account's tokens, with the plaintext of none of them.

    Only the prefix comes back: a list of rows that all read `eris_` is a list
    the reader cannot tell apart, and a token that could be read back would be a
    credential sitting in a page cache.
    """
    return {"items": tokens.list_all(u["id"]), "max": tokens.MAX_TOKENS}


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


# --- auth ---


@app.post("/api/auth/register")
async def register(req: RegisterRequest, res: Response):
    u, err = auth.create_user(req.name, req.email, req.password)
    if err:
        raise HTTPException(400, err)
    res.set_cookie("session", auth.new_session(u["id"]), httponly=True, samesite="lax",
                   max_age=auth.SESSION_TTL, path="/")
    return u


@app.post("/api/auth/login")
async def login(req: LoginRequest, res: Response):
    u = auth.verify_user(req.email, req.password)
    if not u:
        raise HTTPException(401, "email/password salah")
    res.set_cookie("session", auth.new_session(u["id"]), httponly=True, samesite="lax",
                   max_age=auth.SESSION_TTL, path="/")
    return u


@app.post("/api/auth/profile")
async def update_profile(req: ProfileRequest, u: dict = Depends(require_session)):
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "name required")
        auth.set_name(u["id"], name[:40])
    return auth.profile(db_user(u["id"]))


@app.post("/api/auth/password")
async def update_password(req: PasswordRequest, res: Response, u: dict = Depends(require_session)):
    token, err = auth.change_password(u["id"], req.old, req.new)
    if err:
        raise HTTPException(400, err)
    res.set_cookie("session", token, httponly=True, samesite="lax",
                   max_age=auth.SESSION_TTL, path="/")
    return {"ok": True}


@app.get("/api/models")
async def models(u: dict = Depends(require_user)):
    items = await list_models()
    prefs = u.get("prefs") or {}
    default = prefs.get("model") or settings.model_name
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
    caps = await listed_capabilities() or {}
    return {
        "items": items,
        "labels": {mid: model_label(mid) for mid in items},
        "default": default,
        "capabilities": {mid: caps[mid] for mid in items if mid in caps},
        "effort": prefs.get("effort"),
        "ctx": prefs.get("ctx"),
    }


@app.get("/api/effort")
async def effort(model: str | None = None, u: dict = Depends(require_user)):
    """The effort steps this model offers, for the control to draw.

    Probed here rather than carried on `/api/models`, because a probe is a
    round trip and the model list must not wait on one. The answer is cached
    per model, so this is slow once per model and free afterwards.

    The list is never empty: a model with no rungs gets the single fixed step
    the control can still show, and `always` says it cannot be moved.
    """
    prefs = u.get("prefs") or {}
    mid = model or prefs.get("model") or settings.model_name
    ladder = await think_ladder(mid)
    # Nothing could be asked, so the plain pair is offered: it is what the
    # request would carry anyway, and a control that vanished would be a
    # worse answer than one that works.
    rungs = FALLBACK_LADDER if ladder is None else ladder
    if not rungs:
        # This model cannot think at all, so the only honest step is off.
        rungs = [False]
    return {
        "model": mid,
        "efforts": [{"value": i, "label": effort_label(v)} for i, v in enumerate(rungs)],
        "always": len(rungs) == 1,
        "current": rung_index(prefs.get("effort"), rungs),
    }


@app.get("/api/stats")
async def server_stats(model: str | None = None, u: dict = Depends(require_user)):
    """What the model server is holding, for the panel in the composer.

    Read-only and cheap: the endpoint is asked what it has loaded, not asked to
    generate anything, so this can be polled while a chat runs without loading
    a model or spending a token.

    `ctx` is the context this account would ask for, and it is passed rather
    than assumed, because the KV figure is sized against it whenever the model
    is not loaded yet.
    """
    prefs = u.get("prefs") or {}
    mid = model or prefs.get("model") or settings.model_name
    return await model_stats(mid, ctx=int(prefs.get("ctx") or 0) or None)


@app.post("/api/prefs")
async def update_prefs(req: PrefsRequest, u: dict = Depends(require_session)):
    prefs = dict(u.get("prefs") or {})
    if req.model is not None:
        prefs["model"] = req.model[:80]
    if req.effort is not None:
        prefs["effort"] = max(0, int(req.effort))
    if req.ctx is not None:
        prefs["ctx"] = max(0, min(int(req.ctx), 262144))
    if req.system_prompt is not None:
        # Empty string clears the override; chat falls back to the env prompt.
        prompt = req.system_prompt.strip()
        if prompt:
            prefs["system_prompt"] = prompt[:4000]
        else:
            prefs.pop("system_prompt", None)
    auth.set_prefs(u["id"], prefs)
    return {"ok": True, "prefs": prefs}


MEDIA_KINDS = ("avatar", "cover", "model")


def media_url(u: dict, kind: str) -> str | None:
    if kind in ("avatar", "cover"):
        return u.get(kind)
    return (u.get("prefs") or {}).get("model_avatar")


@app.post("/api/auth/media")
async def upload_media(file: UploadFile, kind: str = "avatar", u: dict = Depends(require_session)):
    if kind not in MEDIA_KINDS:
        raise HTTPException(400, "kind must be avatar, cover, or model")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "image required")
    raw = await file.read()
    if len(raw) > 2_000_000:
        raise HTTPException(400, "image too large (max 2MB)")
    ext = encoding.image_extension(file.content_type)
    name = f"{u['id']}-{kind}-{encoding.new_token(4)}.{ext}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(raw)
    old_url = media_url(u, kind)
    auth.set_media(u["id"], kind, f"/uploads/{name}")
    if old_url and old_url.startswith("/uploads/"):
        (MEDIA_DIR / old_url.split("/")[-1]).unlink(missing_ok=True)
    return {"url": f"/uploads/{name}"}


@app.delete("/api/auth/media")
async def delete_media(kind: str = "avatar", u: dict = Depends(require_session)):
    if kind not in MEDIA_KINDS:
        raise HTTPException(400, "kind must be avatar, cover, or model")
    old_url = media_url(u, kind)
    auth.set_media(u["id"], kind, None)
    if old_url and old_url.startswith("/uploads/"):
        (MEDIA_DIR / old_url.split("/")[-1]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/auth/logout")
async def logout(session: str | None = Cookie(None)):
    auth.drop_session(session)
    r = RedirectResponse("/", 303)
    r.delete_cookie("session", path="/")
    return r


@app.get("/api/auth/me")
async def me(u: dict = Depends(require_user)):
    """Who this call is, by cookie or by token.

    It reads rather than writes, so a token is enough: this is the route an MCP
    client uses to confirm its token before it starts speaking.
    """
    return u


# --- health ---


@app.get("/api/health")
async def health():
    """The sidebar's dots. Each dependency is reported as up, down, or unknown."""
    out = {"model": "unknown", "search": "unknown"}
    out["model"] = "up" if await healthy() else "down"
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.searxng_url}/healthz")
            out["search"] = "up" if r.status_code < 400 else "down"
    except Exception:
        out["search"] = "down"
    return out


# --- pages ---


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not auth.get_session(request.cookies.get("session")):
        return RedirectResponse("/auth.html", 303)
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")