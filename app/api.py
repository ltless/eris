import json
from datetime import date
from io import BytesIO

import secrets
from pathlib import Path

import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from pydantic import BaseModel

from . import auth, chats, rag
from .config import settings
from .tools import fetch, search

app = FastAPI(title="Eris")

MEDIA_DIR = Path("static/uploads")


def db_user(user_id):
    row = auth.db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return row


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


class ChatRequest(BaseModel):
    messages: list[Message]
    use_rag: bool = True
    model: str | None = None
    thinking: bool = False
    mode: str = "agentic"
    chat_id: str | None = None


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


def require_user(session: str | None = Cookie(None)):
    u = auth.get_session(session)
    if not u:
        raise HTTPException(401, "Login required")
    return u


def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def to_native(messages):
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            text = "".join(p.get("text", "") for p in c if p.get("type") == "text")
            imgs = [p["image_url"]["url"].split(",", 1)[-1] for p in c if p.get("type") == "image_url"]
            mm = {"role": m["role"], "content": text}
            if imgs:
                mm["images"] = imgs
            out.append(mm)
        else:
            out.append(m)
    return out


async def stream_completion(messages, model=None, thinking=False, tools=None, ctx=None):
    payload = {
        "model": model or settings.model_name,
        "messages": to_native(messages),
        "stream": True,
        "think": bool(thinking),
    }
    if ctx:
        payload["options"] = {"num_ctx": ctx}
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {settings.model_api_key}"}
    url = settings.model_base_url.replace("/v1", "") + "/api/chat"
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as c:
        async with c.stream("POST", url, json=payload, headers=headers) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode(errors="replace")
                raise HTTPException(r.status_code, f"Model server error: {body}")
            async for line in r.aiter_lines():
                if not line:
                    continue
                try:
                    msg = json.loads(line).get("message") or {}
                except json.JSONDecodeError:
                    continue
                if msg.get("content"):
                    yield ("delta", msg["content"])
                if msg.get("thinking"):
                    yield ("thinking", msg["thinking"])
                if msg.get("tool_calls"):
                    yield ("tool_calls", msg["tool_calls"])


@app.post("/api/chat")
async def chat(req: ChatRequest, u: dict = Depends(require_user)):
    prefs = u.get("prefs") or {}
    model = req.model or prefs.get("model") or None
    thinking = bool(req.thinking or prefs.get("thinking"))
    ctx = int(prefs.get("ctx") or 0) or None
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    query = ""
    for m in reversed(messages):
        if m["role"] != "user":
            continue
        c = m["content"]
        if isinstance(c, str):
            query = c
        elif isinstance(c, list):
            query = next(
                (
                    p.get("text", "")
                    for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                ),
                "",
            )
        break
    sources = []
    sys_prompt = (
        f"{settings.system_prompt}\n\nToday is {date.today().isoformat()}. "
        "For anything current or recent, answer only from the search "
        "results you receive, never from training memory."
    )
    if req.use_rag:
        try:
            sources = await rag.retrieve(u["id"], query)
        except Exception:
            sources = []
    if sources:
        context = "\n\n".join(
            f"[{i + 1}] ({s['source']})\n{s['text']}" for i, s in enumerate(sources)
        )
        sys_prompt += (
            "\n\nUse the context below when relevant. "
            "Cite sources as [n].\n\nContext:\n" + context
        )
    messages = [{"role": "system", "content": sys_prompt}] + messages

    async def gen():
        used = [{"source": s["source"], "kind": "doc"} for s in sources]
        if sources:
            yield sse({"type": "sources", "items": used})
        answer = []
        think_parts = [""]
        web_groups = []
        msgs = messages
        try:
            for round_no in range(settings.max_tool_rounds):
                calls = []
                tools = [search.TOOL, fetch.TOOL]
                async for kind, text in stream_completion(
                    msgs, model=model, thinking=thinking, tools=tools, ctx=ctx
                ):
                    if kind == "tool_calls":
                        calls = text
                        continue
                    if kind == "delta":
                        answer.append(text)
                    elif kind == "thinking":
                        think_parts[-1] += text
                    yield sse({"type": kind, "text": text})
                if not calls:
                    break
                valid = []
                for call in calls:
                    fn = call.get("function") or {}
                    if fn.get("name") not in (
                        search.TOOL["function"]["name"],
                        fetch.TOOL["function"]["name"],
                    ):
                        continue
                    valid.append(call)
                    if fn.get("name") == search.TOOL["function"]["name"]:
                        args = fn.get("arguments") or {}
                        yield sse({"type": "tool", "r": round_no, "query": str(args.get("query", "") or "")})
                if not valid:
                    break
                msgs = msgs + [{"role": "assistant", "content": "", "tool_calls": valid}]
                think_parts.append("")
                for call in valid:
                    name = call["function"].get("name")
                    if name == fetch.TOOL["function"]["name"]:
                        text = await fetch.run(call["function"]["arguments"])
                        tool_msg = {"role": "tool", "content": text}
                        if call.get("id"):
                            tool_msg["tool_call_id"] = call["id"]
                        msgs.append(tool_msg)
                        continue
                    q = str((call["function"].get("arguments") or {}).get("query", "") or "")
                    web_groups.append({"r": round_no, "query": q, "items": []})
                    hits = await search.run(call["function"]["arguments"])
                    tool_msg = {"role": "tool", "content": search.render(hits)}
                    if call.get("id"):
                        tool_msg["tool_call_id"] = call["id"]
                    msgs.append(tool_msg)
                    if hits:
                        web = [{"source": h["url"] or h["title"], "title": h["title"] or h["url"], "kind": "web"} for h in hits]
                        web_groups[-1]["items"] = web
                        used.extend(web)
                        yield sse({"type": "sources", "items": web})
        except HTTPException as e:
            yield sse({"type": "error", "message": e.detail})
        except httpx.HTTPError as e:
            yield sse({"type": "error", "message": f"Cannot reach model server: {e}"})
        if req.chat_id and (query or answer):
            if query:
                chats.save_turn(u["id"], req.chat_id, "user", query)
                chats.touch_title(req.chat_id, query)
            if answer:
                chats.save_turn(
                    u["id"], req.chat_id, "assistant", "".join(answer),
                    thinking=[t for t in think_parts if t.strip()] or None, sources=used,
                    web=[g for g in web_groups if g["items"]],
                )
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


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
async def chat_patch(chat_id: str, req: ChatPatch, u: dict = Depends(require_user)):
    if req.title is None and req.pinned is None:
        raise HTTPException(400, "nothing to update")
    chats.set_meta(u["id"], chat_id, req.title, req.pinned)
    return {"ok": True}


@app.delete("/api/chats/{chat_id}")
async def chat_delete(chat_id: str, u: dict = Depends(require_user)):
    chats.delete(u["id"], chat_id)
    return {"ok": True}


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile, u: dict = Depends(require_user)):
    raw = await file.read()
    name = (file.filename or "").lower()
    if name.endswith(".pdf") or file.content_type == "application/pdf":
        try:
            text = "\n".join(
                (page.extract_text() or "") for page in PdfReader(BytesIO(raw)).pages
            )
        except Exception:
            raise HTTPException(400, "Cannot extract text from PDF")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    if not text.strip():
        raise HTTPException(400, "Empty document")
    try:
        n = await rag.add_document(u["id"], file.filename, text)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Embedding model unavailable: {e}")
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")
    return {"source": file.filename, "chunks": n}


@app.post("/api/auth/register")
async def register(req: RegisterRequest, res: Response):
    u, err = auth.create_user(req.name, req.email, req.password)
    if err:
        raise HTTPException(400, err)
    res.set_cookie("session", auth.new_session(u["id"]), httponly=True, samesite="lax", max_age=auth.SESSION_TTL, path="/")
    return u


@app.post("/api/auth/login")
async def login(req: LoginRequest, res: Response):
    u = auth.verify_user(req.email, req.password)
    if not u:
        raise HTTPException(401, "email/password salah")
    res.set_cookie("session", auth.new_session(u["id"]), httponly=True, samesite="lax", max_age=auth.SESSION_TTL, path="/")
    return u


@app.post("/api/auth/profile")
async def update_profile(req: ProfileRequest, u: dict = Depends(require_user)):
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "name required")
        auth.set_name(u["id"], name[:40])
    return auth.profile(db_user(u["id"]))


@app.post("/api/auth/password")
async def update_password(req: PasswordRequest, res: Response, u: dict = Depends(require_user)):
    token, err = auth.change_password(u["id"], req.old, req.new)
    if err:
        raise HTTPException(400, err)
    res.set_cookie("session", token, httponly=True, samesite="lax", max_age=auth.SESSION_TTL, path="/")
    return {"ok": True}


@app.get("/api/models")
async def models(u: dict = Depends(require_user)):
    items = []
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                f"{settings.model_base_url}/models",
                headers={"Authorization": f"Bearer {settings.model_api_key}"},
            )
            r.raise_for_status()
            items = [m["id"] for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        pass
    default = (u.get("prefs") or {}).get("model") or settings.model_name
    if default not in items:
        items.insert(0, default)
    return {"items": items, "default": default, "thinking": bool((u.get("prefs") or {}).get("thinking")), "ctx": (u.get("prefs") or {}).get("ctx")}


@app.post("/api/prefs")
async def update_prefs(req: PrefsRequest, u: dict = Depends(require_user)):
    prefs = dict(u.get("prefs") or {})
    if req.model is not None:
        prefs["model"] = req.model[:80]
    if req.thinking is not None:
        prefs["thinking"] = bool(req.thinking)
    if req.ctx is not None:
        prefs["ctx"] = max(0, min(int(req.ctx), 262144))
    auth.set_prefs(u["id"], prefs)
    return {"ok": True, "prefs": prefs}


@app.post("/api/auth/media")
async def upload_media(file: UploadFile, kind: str = "avatar", u: dict = Depends(require_user)):
    if kind not in ("avatar", "cover"):
        raise HTTPException(400, "kind must be avatar or cover")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "image required")
    raw = await file.read()
    if len(raw) > 2_000_000:
        raise HTTPException(400, "image too large (max 2MB)")
    ext = "png" if "png" in (file.content_type or "") else "jpg" if "jpeg" in (file.content_type or "") else "webp" if "webp" in (file.content_type or "") else "jpg"
    name = f"{u['id']}-{kind}-{secrets.token_hex(4)}.{ext}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(raw)
    old_url = u[kind]
    auth.set_media(u["id"], kind, f"/uploads/{name}")
    if old_url and old_url.startswith("/uploads/"):
        (MEDIA_DIR / old_url.split("/")[-1]).unlink(missing_ok=True)
    return {"url": f"/uploads/{name}"}


@app.post("/api/auth/logout")
async def logout(session: str | None = Cookie(None)):
    auth.drop_session(session)
    r = RedirectResponse("/", 303)
    r.delete_cookie("session", path="/")
    return r


@app.get("/api/auth/me")
async def me(session: str | None = Cookie(None)):
    u = auth.get_session(session)
    if not u:
        raise HTTPException(401, "Login required")
    return u


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not auth.get_session(request.cookies.get("session")):
        return RedirectResponse("/auth.html", 303)
    return FileResponse("static/index.html")


app.mount("/", StaticFiles(directory="static", html=True), name="static")