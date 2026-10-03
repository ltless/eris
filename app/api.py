import json
from datetime import date
from io import BytesIO

import httpx
from fastapi import Cookie, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from pydantic import BaseModel

from . import auth, rag
from .config import settings
from .tools import search

app = FastAPI(title="Eris")


class Message(BaseModel):
    role: str
    content: str | list


class ChatRequest(BaseModel):
    messages: list[Message]
    use_rag: bool = True
    model: str | None = None
    thinking: bool = False
    mode: str = "agentic"
    chat_id: str | None = None


class DocumentRequest(BaseModel):
    source: str
    text: str


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


def me_from_cookie(session: str | None = Cookie(None)):
    return auth.get_session(session)


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


async def stream_completion(messages, model=None, thinking=False, tools=None):
    payload = {
        "model": model or settings.model_name,
        "messages": to_native(messages),
        "stream": True,
        "think": bool(thinking),
    }
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {settings.model_api_key}"}
    url = settings.model_base_url.replace("/v1", "") + "/api/chat"
    async with httpx.AsyncClient(timeout=None) as c:
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
async def chat(req: ChatRequest, session: str | None = Cookie(None)):
    u = require_user(session)
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
            sources = await rag.retrieve(query)
        except Exception:
            sources = []
        if sources:
            context = "\n\n".join(
                f"[{i + 1}] ({s['source']})\n{s['text']}" for i, s in enumerate(sources)
            )
            messages = [
                {
                    "role": "system",
                    "content": (
                        sys_prompt
                        + "\n\nUse the context below when relevant. "
                        "Cite sources as [n].\n\nContext:\n"
                        + context
                    ),
                }
            ] + messages
        else:
            messages = [{"role": "system", "content": sys_prompt}] + messages
    else:
        messages = [{"role": "system", "content": sys_prompt}] + messages

    async def gen():
        used = [{"source": s["source"], "kind": "doc"} for s in sources]
        if sources:
            yield sse({"type": "sources", "items": sources})
        answer = []
        think_parts = []
        msgs = messages
        try:
            # ponytail: one tool round. Raise when a second tool needs its own step.
            for round_no in range(2):
                calls = []
                tools = [search.TOOL] if round_no == 0 else None
                async for kind, text in stream_completion(msgs, req.model, req.thinking, tools):
                    if kind == "tool_calls":
                        calls = text
                        continue
                    if kind == "delta":
                        answer.append(text)
                    elif kind == "thinking":
                        think_parts.append(text)
                    yield sse({"type": kind, "text": text})
                if not calls:
                    break
                valid = []
                for call in calls:
                    fn = call.get("function") or {}
                    args = fn.get("arguments") or {}
                    if fn.get("name") != search.TOOL["function"]["name"]:
                        continue
                    valid.append(call)
                    yield sse({"type": "tool", "query": str(args.get("query", "") or "")})
                if not valid:
                    break
                msgs = msgs + [{"role": "assistant", "content": "", "tool_calls": valid}]
                for call in valid:
                    hits = await search.run(call["function"]["arguments"])
                    msgs.append({"role": "tool", "content": search.render(hits)})
                    if hits:
                        web = [{"source": h["url"] or h["title"], "title": h["title"] or h["url"], "kind": "web"} for h in hits]
                        used.extend(web)
                        yield sse({"type": "sources", "items": web})
        except HTTPException as e:
            yield sse({"type": "error", "message": e.detail})
        except httpx.HTTPError as e:
            yield sse({"type": "error", "message": f"Cannot reach model server: {e}"})
        if req.chat_id and (query or answer):
            try:
                if query:
                    rag.save_turn(u["id"], req.chat_id, "user", query)
                if answer:
                    rag.save_turn(
                        u["id"], req.chat_id, "assistant", "".join(answer),
                        thinking="".join(think_parts) or None, sources=used,
                    )
            except Exception:
                pass
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/chats")
async def chats(session: str | None = Cookie(None)):
    u = require_user(session)
    try:
        return {"items": rag.chat_list(u["id"])}
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")


@app.get("/api/chats/{chat_id}")
async def chat_history(chat_id: str, session: str | None = Cookie(None)):
    u = require_user(session)
    try:
        return {"items": rag.chat_turns(u["id"], chat_id)}
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")


class ChatPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None


@app.patch("/api/chats/{chat_id}")
async def chat_patch(chat_id: str, req: ChatPatch, session: str | None = Cookie(None)):
    u = require_user(session)
    if req.title is None and req.pinned is None:
        raise HTTPException(400, "nothing to update")
    try:
        rag.chat_set_meta(u["id"], chat_id, req.title, req.pinned)
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")
    return {"ok": True}


@app.delete("/api/chats/{chat_id}")
async def chat_delete(chat_id: str, session: str | None = Cookie(None)):
    u = require_user(session)
    try:
        rag.chat_delete(u["id"], chat_id)
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")
    return {"ok": True}


@app.get("/api/models")
async def models():
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
    if settings.model_name not in items:
        items.insert(0, settings.model_name)
    return {"items": items, "default": settings.model_name}


@app.get("/api/documents")
def documents():
    try:
        return {"items": rag.list_documents()}
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")


@app.post("/api/documents")
async def add_document(req: DocumentRequest):
    if not req.text.strip():
        raise HTTPException(400, "Empty document")
    try:
        n = await rag.add_document(req.source, req.text)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Embedding model unavailable: {e}")
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")
    return {"source": req.source, "chunks": n}


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile):
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
        n = await rag.add_document(file.filename, text)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Embedding model unavailable: {e}")
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")
    return {"source": file.filename, "chunks": n}


@app.delete("/api/documents")
def remove_document(source: str):
    try:
        rag.delete_document(source)
    except Exception as e:
        raise HTTPException(503, f"ChromaDB unavailable: {e}")
    return {"deleted": source}


@app.get("/api/health")
async def health():
    status = {"model": False, "chroma": False}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.model_base_url}/models")
            status["model"] = r.status_code < 400
    except Exception:
        pass
    try:
        rag.client().heartbeat()
        status["chroma"] = True
    except Exception:
        pass
    return status


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