"""HTTP surface: routes, authentication, validation, and SSE.

This layer holds no execution logic. `/api/chat` builds a RunRequest, drives
the shared engine, and translates its events into the SSE frames the browser
already understands; the engine does the rest.

The old transports were also in this file. They now live in `app/models`
(`stream_completion`, `parse_chunk`, `to_native`, `merge_tool_calls`,
`normalize_call`, `chat_url`) and are re-exported here, because the routes and
the existing tests still refer to them by these names.
"""

import json
import secrets
from datetime import date

import httpx
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, chats, encoding, tokens
from .config import settings
from .models import (
    ModelError,
    chat_url,
    list_models,
    merge_tool_calls,
    model_label,
    normalize_call,
    parse_chunk,
    stream_completion,
    to_native,
)
from .runtime import events as ev
from .runtime.budgets import Budget
from .runtime.context import ContextBudget, assemble
from .runtime.engine import Engine, RunRequest
from .runtime.events import Emitter
from .tools import builtin as builtin_tools
from .tools import policy, search
from .tools.registry import ToolContext, registry

app = FastAPI(title="Eris")

ROOT = encoding.ROOT
STATIC_DIR = encoding.STATIC_DIR
MEDIA_DIR = encoding.MEDIA_DIR

def _register_tools():
    """Register built-in tools once at import."""
    builtin_tools.register_builtins()


_register_tools()


# --- auth helpers ---


def db_user(user_id):
    row = auth.db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return row


def require_user(session: str | None = Cookie(None),
                 authorization: str | None = Header(None)):
    """The account making this call, from a cookie or a bearer token.

    A browser holds a session cookie. A script, an editor, or an agent speaking
    MCP cannot, so it presents a token instead; `tokens.resolve` is the only
    place the plaintext is ever compared.
    """
    u = auth.get_session(session)
    if u:
        return u
    if authorization and authorization.lower().startswith("bearer "):
        user_id = tokens.resolve(authorization.split(" ", 1)[1].strip())
        row = db_user(user_id) if user_id is not None else None
        if row:
            return auth.profile(row)
    raise HTTPException(401, "Login required")


def require_session(session: str | None = Cookie(None)):
    """The account, from a session cookie only.

    A token is read scope: it may ask the app questions, and it may not rewrite
    the account that issued it. Every route that changes the account itself —
    its name, its credentials, its preferences, its tokens — stands behind this
    instead, so a leaked token cannot be escalated into a takeover.
    """
    u = auth.get_session(session)
    if not u:
        raise HTTPException(401, "Login required")
    return u


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
    thinking: bool | None = None
    ctx: int | None = None
    system_prompt: str | None = None


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    thinking: bool = False
    chat_id: str | None = None
    regenerate: bool = False


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


# --- SSE ---


def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def sse_frame(event):
    """One runtime event, in the frame the browser already parses.

    The names differ between the two eras of this app — `tool.started` here,
    `tool` there — and the frontend is not being rewritten, so the mapping is
    here, in one place, with the event still carrying its own fields.
    """
    d = event.data or {}
    t = event.type
    if t == ev.MODEL_DELTA:
        return {"type": "delta", "text": d.get("text", "")}
    if t == ev.MODEL_THINKING:
        return {"type": "thinking", "text": d.get("text", "")}
    if t == ev.SOURCE_FOUND:
        return {"type": "sources", "items": [{
            "n": d.get("n"), "source": d.get("source", ""), "title": d.get("title", ""),
            "kind": d.get("kind", "web"),
        }]}
    if t == ev.TOOL_STARTED and d.get("tool") == "web_search":
        return {"type": "tool", "r": d.get("round", 0),
                "query": str((d.get("args") or {}).get("query", "") or "")}
    if t == ev.TOOL_FAILED:
        return {"type": "tool_error", "tool": d.get("tool", ""),
                "code": d.get("code", ""), "message": d.get("message", "")}
    if t == ev.APPROVAL_REQUIRED:
        return {"type": "approval", "tool": d.get("tool", ""), "message": d.get("message", "")}
    if t in (ev.TASK_FAILED, ev.TASK_CANCELLED):
        return {"type": "error", "message": d.get("message") or d.get("reason") or t}
    return None


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


def _build_request(req: ChatRequest, u):
    prefs = u.get("prefs") or {}
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
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
    return RunRequest(
        messages=[{"role": "system", "content": system}] + messages,
        model=req.model or prefs.get("model") or None,
        thinking=bool(req.thinking or prefs.get("thinking")),
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
    request = _build_request(req, u)
    engine = Engine(request, emitter=Emitter())

    async def gen():
        if req.regenerate:
            chats.drop_last_assistant(u["id"], req.chat_id)
        web_groups = []
        try:
            async for event in engine.run():
                frame = sse_frame(event)
                if frame is not None:
                    if event.type == ev.TOOL_STARTED and frame.get("type") == "tool":
                        web_groups.append({"r": frame["r"], "query": frame["query"], "items": []})
                    elif event.type == ev.SOURCE_FOUND and frame.get("type") == "sources":
                        items = [i for i in frame["items"] if i.get("kind") == "web"]
                        if items:
                            if web_groups:
                                web_groups[-1]["items"].extend(items)
                            else:
                                web_groups.append({"r": 0, "query": "", "items": items})
                            yield sse({"type": "sources", "items": items})
                    if frame.get("type") != "sources":
                        yield sse(frame)
                if event.type == ev.MODEL_DELTA:
                    continue
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
    return {
        "items": items,
        "labels": {mid: model_label(mid) for mid in items},
        "default": default,
        "thinking": bool(prefs.get("thinking")),
        "ctx": prefs.get("ctx"),
    }


@app.post("/api/prefs")
async def update_prefs(req: PrefsRequest, u: dict = Depends(require_session)):
    prefs = dict(u.get("prefs") or {})
    if req.model is not None:
        prefs["model"] = req.model[:80]
    if req.thinking is not None:
        prefs["thinking"] = bool(req.thinking)
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
    from .models import healthy as model_healthy

    out = {"model": "unknown", "search": "unknown"}
    out["model"] = "up" if await model_healthy() else "down"
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.searxng_url}/healthz")
            out["search"] = "up" if r.status_code < 400 else "down"
    except Exception:
        out["search"] = "down"
    return out


# --- MCP ---

# The Model Context Protocol over Streamable HTTP: one endpoint, JSON-RPC over
# POST. The tools are the ones already registered — this exposes them, it does
# not define any — so a client sees exactly what a chat turn sees and a tool is
# added in one place.
MCP_VERSION = "2025-06-18"
# A version older than the server's own. A request without the version header
# is assumed to speak this one, which is what the spec says to assume.
MCP_FALLBACK = "2025-03-26"
MCP_SESSIONS = 64
_mcp_sessions: dict = {}

MCP_METHODS = ("initialize", "ping", "tools/list", "tools/call")


def _rpc_error(rpc_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


def _rpc_result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _mcp_tools():
    """The registered tools, in the shape MCP names them.

    `inputSchema` is the registry's own JSON Schema — the same object the model
    is offered — so a tool cannot describe itself one way to the model and
    another way to an MCP client.
    """
    out = []
    for t in registry.all():
        if "chat" not in t.tags:
            continue
        out.append({
            "name": t.name,
            "description": t.description,
            "inputSchema": t.parameters,
            "annotations": {
                "readOnlyHint": t.permission == policy.READ_ONLY,
                "destructiveHint": t.permission == policy.DESTRUCTIVE,
                "idempotentHint": t.permission == policy.READ_ONLY,
            },
        })
    return out


def _mcp_content(result):
    """A ToolResult as MCP's content list."""
    if result.success:
        text = result.text if result.text is not None else json.dumps(
            result.data or {}, ensure_ascii=False, indent=2
        )
    else:
        err = result.error or {}
        text = err.get("message") or err.get("code") or "the tool failed"
    return [{"type": "text", "text": str(text)}]


async def _mcp_call(user, params):
    """Run one tool for the account the request authenticated as.

    A refusal is an MCP tool error, not a protocol error: the call was well
    formed, the tool said no. That distinction is the client's, and it is the
    difference between fixing the request and reading the message.
    """
    name = str((params or {}).get("name") or "")
    args = (params or {}).get("arguments") or {}
    if not isinstance(args, dict):
        return None, "Arguments must be an object"
    if registry.get(name) is None:
        return None, f"Unknown tool: {name}"
    ctx = ToolContext(user_id=user["id"], approved=set())
    result = await registry.call(name, args, ctx)
    return {"content": _mcp_content(result), "isError": not result.success,
            "structuredContent": result.data if result.data else None}, None


async def _mcp_handle(user, body):
    """One JSON-RPC message, from a parsed body to a response body.

    Returns `(payload, http_status)`. A notification has no id and gets a 202
    with no body, because a reply to one is a protocol violation — including a
    notification this server does not implement, which is why the notification
    check comes before the method list.
    """
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}, 200
    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if isinstance(method, str) and method.startswith("notifications/"):
        return None, 202
    if method not in MCP_METHODS:
        return _rpc_error(rpc_id, -32601, f"Method not found: {method}"), 200
    if method == "initialize":
        return _rpc_result(rpc_id, {
            "protocolVersion": MCP_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "eris", "version": "1.0"},
            "instructions": (
                "These are Eris's own tools, running as the account that owns this "
                "token. A tool that changes state needs an approval the token cannot "
                "give, so it will refuse; reads and searches run."
            ),
        }), 200
    if method == "ping":
        return _rpc_result(rpc_id, {}), 200
    if method == "tools/list":
        return _rpc_result(rpc_id, {"tools": _mcp_tools()}), 200
    result, err = await _mcp_call(user, params)
    if err:
        return _rpc_error(rpc_id, -32602, err), 200
    return _rpc_result(rpc_id, result), 200


def _mcp_origin_ok(origin, host):
    """Whether an Origin header names this server.

    A browser sends Origin on a cross-site request, and this endpoint answers
    with the caller's own tools. A page on another host must not be able to
    drive them through a visitor's browser, so a mismatched Origin is refused
    outright rather than left to CORS.
    """
    if not origin:
        return True
    try:
        from urllib.parse import urlsplit

        return urlsplit(origin).netloc.split(":")[0] == (host or "").split(":")[0]
    except Exception:
        return False


@app.post("/mcp")
async def mcp(request: Request, u: dict = Depends(require_user)):
    if not _mcp_origin_ok(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(403, "origin not allowed")
    accept = request.headers.get("accept") or ""
    if "application/json" not in accept or "text/event-stream" not in accept:
        raise HTTPException(406, "Accept must include application/json and text/event-stream")
    version = (request.headers.get("mcp-protocol-version") or "").strip()
    if version and version not in (MCP_VERSION, MCP_FALLBACK):
        raise HTTPException(400, f"unsupported MCP-Protocol-Version: {version}")

    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(413, "request too large")
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        return JSONResponse(
            _rpc_error(None, -32700, "Parse error"), status_code=200
        )

    session_id = request.headers.get("mcp-session-id")
    if session_id and session_id not in _mcp_sessions:
        # Terminated or never issued: the client has to initialize again.
        raise HTTPException(404, "unknown MCP session")
    if session_id and _mcp_sessions[session_id] != u["id"]:
        raise HTTPException(403, "this session belongs to another account")

    payload, status = await _mcp_handle(u, body)
    if payload is None:
        return Response(status_code=status)
    headers = {}
    if body.get("method") == "initialize":
        # The id is issued on the initialize response and required afterwards.
        sid = secrets.token_urlsafe(18)
        _mcp_sessions[sid] = u["id"]
        while len(_mcp_sessions) > MCP_SESSIONS:
            _mcp_sessions.pop(next(iter(_mcp_sessions)))
        headers["Mcp-Session-Id"] = sid
    return JSONResponse(payload, status_code=status, headers=headers)


@app.get("/mcp")
async def mcp_get():
    """No server-initiated stream: this server only answers.

    The spec wants either an SSE stream or a 405 here, and a second channel of
    unsolicited events is not what this server has to say.
    """
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@app.delete("/mcp")
async def mcp_delete(request: Request, u: dict = Depends(require_user)):
    session_id = request.headers.get("mcp-session-id")
    if session_id:
        _mcp_sessions.pop(session_id, None)
    return Response(status_code=204)


# --- pages ---


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not auth.get_session(request.cookies.get("session")):
        return RedirectResponse("/auth.html", 303)
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")