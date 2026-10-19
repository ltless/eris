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

from . import auth, chats, library, rag
from .config import settings
from .tools import calc, doc_search, fetch, search

app = FastAPI(title="Eris")

# Anchor asset paths to the repo root so uvicorn works from any CWD.
ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
MEDIA_DIR = STATIC_DIR / "uploads"


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
    system_prompt: str | None = None


class ChatRequest(BaseModel):
    messages: list[Message]
    use_rag: bool = True
    model: str | None = None
    thinking: bool = False
    mode: str = "agentic"
    chat_id: str | None = None
    regenerate: bool = False


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


def tool_result(call, content):
    m = {"role": "tool", "content": content}
    if call.get("id"):
        m["tool_call_id"] = call["id"]
    return m


def merge_tool_calls(calls, incoming):
    """The server streams one tool call per line, each with its own id, so a
    round can carry several. A repeated id replaces the earlier copy instead of
    running the same call twice."""
    for call in incoming:
        for i, seen in enumerate(calls):
            if call.get("id") and seen.get("id") == call["id"]:
                calls[i] = call
                break
        else:
            calls.append(call)


TOOL_NAMES = {
    t["function"]["name"] for t in (search.TOOL, fetch.TOOL, calc.TOOL, doc_search.TOOL)
}


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


def chat_url():
    """The model talks on the native /api/chat path.

    MODEL_BASE_URL keeps the OpenAI-style /v1 suffix because embeddings use
    /v1/embeddings; chat itself is the native NDJSON stream that the parser
    below expects, so the suffix is dropped for this one call.
    """
    base = settings.model_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/api/chat"


def normalize_call(call):
    """Native streams send tool arguments as an object, OpenAI-style servers
    send a JSON string. Runners downstream expect the object."""
    fn = call.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
        call = {**call, "function": {**fn, "arguments": args}}
    return call


def parse_chunk(line):
    """One streamed line -> list of (kind, value) events.

    The native stream is bare JSON with a `message` object, one per line.
    An OpenAI-compatible server answers instead with SSE frames like
    `data: {"choices": [{"delta": {...}}]}`, where thinking arrives as
    `reasoning_content` instead of `thinking`. Blank lines, keep-alives,
    `[DONE]` and partial JSON are skipped rather than raising, so a stray
    frame can never end the turn with an empty answer.
    """
    line = line.strip()
    if not line:
        return []
    if line.startswith("data:"):
        line = line[len("data:") :].strip()
        if line == "[DONE]":
            return []
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        return []
    msg = chunk.get("message")
    if msg is None:
        choices = chunk.get("choices") or []
        msg = (choices[0].get("delta") or {}) if choices else {}
    events = []
    if msg.get("content"):
        events.append(("delta", msg["content"]))
    think = msg.get("thinking") or msg.get("reasoning_content")
    if think:
        events.append(("thinking", think))
    if msg.get("tool_calls"):
        events.append(("tool_calls", [normalize_call(c) for c in msg["tool_calls"]]))
    return events


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
    url = chat_url()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as c:
        async with c.stream("POST", url, json=payload, headers=headers) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode(errors="replace")
                raise HTTPException(r.status_code, f"Model server error: {body}")
            async for line in r.aiter_lines():
                for event in parse_chunk(line):
                    yield event


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
    base_prompt = (prefs.get("system_prompt") or "").strip() or settings.system_prompt
    sys_prompt = (
        f"{base_prompt}\n\nToday is {date.today().isoformat()}. "
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
    if req.mode == "deep":
        sys_prompt += (
            "\n\nDeep research mode: run focused searches, read the pages, "
            "and if coverage is still thin, search again with different terms "
            "before answering. Only claim what you fetched; state gaps plainly."
        )
    rounds = settings.deep_tool_rounds if req.mode == "deep" else settings.max_tool_rounds
    messages = [{"role": "system", "content": sys_prompt}] + messages

    async def gen():
        if req.regenerate:
            chats.drop_last_assistant(u["id"], req.chat_id)
        used = [{"n": i + 1, "source": s["source"], "kind": "doc"} for i, s in enumerate(sources)]
        next_n = len(used)
        if sources:
            yield sse({"type": "sources", "items": used})
        answer = []
        think_parts = [""]
        web_groups = []
        msgs = messages
        try:
            # The budget counts tool rounds, not the whole turn. Once they are
            # spent the model gets one more pass with no tools at all, so it
            # answers from what the searches returned instead of searching until
            # the loop runs out and the browser is left with only search rows.
            for round_no in range(rounds + 1):
                final = round_no == rounds
                calls = []
                tools = None if final else [search.TOOL, fetch.TOOL, calc.TOOL, doc_search.TOOL]
                async for kind, text in stream_completion(
                    msgs, model=model, thinking=thinking, tools=tools, ctx=ctx
                ):
                    if kind == "tool_calls":
                        merge_tool_calls(calls, text)
                        continue
                    if kind == "delta":
                        answer.append(text)
                    elif kind == "thinking":
                        think_parts[-1] += text
                    yield sse({"type": kind, "text": text})
                if final or not calls:
                    break
                valid = []
                for call in calls:
                    fn = call.get("function") or {}
                    name = fn.get("name")
                    if name not in TOOL_NAMES:
                        continue
                    valid.append(call)
                    if name == search.TOOL["function"]["name"]:
                        args = fn.get("arguments") or {}
                        yield sse({"type": "tool", "r": round_no, "query": str(args.get("query", "") or "")})
                if not valid:
                    break
                msgs = msgs + [{"role": "assistant", "content": "", "tool_calls": valid}]
                think_parts.append("")
                for call in valid:
                    name = call["function"].get("name")
                    args = call["function"].get("arguments")
                    if name == search.TOOL["function"]["name"]:
                        q = str((args or {}).get("query", "") or "")
                        web_groups.append({"r": round_no, "query": q, "items": []})
                        hits = await search.run(args)
                        msgs.append(tool_result(call, search.render(hits, next_n)))
                        if hits:
                            web = [
                                {"n": next_n + j + 1, "source": h["url"] or h["title"], "title": h["title"] or h["url"], "kind": "web"}
                                for j, h in enumerate(hits)
                            ]
                            next_n += len(hits)
                            web_groups[-1]["items"] = web
                            used.extend(web)
                            yield sse({"type": "sources", "items": web})
                    elif name == doc_search.TOOL["function"]["name"]:
                        docs = await doc_search.run(u["id"], args)
                        msgs.append(tool_result(call, doc_search.render(docs, next_n)))
                        if docs:
                            items = [{"n": next_n + j + 1, "source": d["source"], "kind": "doc"} for j, d in enumerate(docs)]
                            next_n += len(docs)
                            used.extend(items)
                            yield sse({"type": "sources", "items": items})
                    elif name == fetch.TOOL["function"]["name"]:
                        msgs.append(tool_result(call, await fetch.run(args)))
                    else:
                        msgs.append(tool_result(call, await calc.run(args)))
        except HTTPException as e:
            yield sse({"type": "error", "message": e.detail})
        except httpx.HTTPError as e:
            yield sse({"type": "error", "message": f"Cannot reach model server: {e}"})
        else:
            yield "data: [DONE]\n\n"
        finally:
            # Persist even if the client hit Stop mid-stream: the turn happened
            # and should still be there after a refresh. No yields allowed here.
            if req.chat_id:
                if query and not req.regenerate:
                    chats.save_turn(u["id"], req.chat_id, "user", query)
                    chats.touch_title(req.chat_id, query)
                if answer:
                    chats.save_turn(
                        u["id"], req.chat_id, "assistant", "".join(answer),
                        thinking=[t for t in think_parts if t.strip()] or None, sources=used,
                        web=[g for g in web_groups if g["items"]],
                    )

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


MAX_DOC_BYTES = 25_000_000
# Inspect ships a document's first chunks, never all of them.
INSPECT_CHUNKS = 50


def vector_error(e):
    """One mapping for both services: the embedder is an HTTP call, Chroma is
    another host. Anything else that breaks here is the vector store."""
    if isinstance(e, httpx.HTTPError):
        return HTTPException(502, f"Embedding model unavailable: {e}")
    return HTTPException(503, f"ChromaDB unavailable: {e}")


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile, u: dict = Depends(require_user)):
    raw = await file.read()
    if len(raw) > MAX_DOC_BYTES:
        raise HTTPException(413, "document too large (max 25MB)")
    label = file.filename or "upload"
    name = label.lower()
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
    doc_id = secrets.token_hex(16)
    try:
        n = await rag.add_document(u["id"], label, text, doc_id=doc_id)
    except Exception as e:
        raise vector_error(e)
    # The row is written only after the chunks are stored, so a failed index
    # cannot leave a row pointing at chunks that were never written.
    try:
        library.add(u["id"], doc_id, label, len(raw), n)
    except Exception:
        rag.discard(u["id"], doc_id)
        raise HTTPException(500, "Could not record the upload")
    try:
        # Same filename means the same document, re-indexed. This runs after the
        # new copy is fully in place, so an upload never deletes what it cannot
        # replace.
        replaced = rag.retire_previous(u["id"], label, keep=doc_id)
    except Exception as e:
        rag.discard(u["id"], doc_id)
        raise vector_error(e)
    return {"id": doc_id, "name": label, "chunks": n, "replaced": replaced}


@app.get("/api/documents")
async def list_documents(u: dict = Depends(require_user)):
    try:
        return {"items": rag.list_documents(u["id"])}
    except Exception as e:
        raise vector_error(e)


@app.get("/api/documents/{doc_id}")
async def document_detail(doc_id: str, u: dict = Depends(require_user)):
    try:
        row = rag.find_document(u["id"], doc_id)
        if not row:
            raise HTTPException(404, "unknown document")
        passages = rag.document_chunks(u["id"], doc_id, INSPECT_CHUNKS)
    except HTTPException:
        raise
    except Exception as e:
        raise vector_error(e)
    return {**row, "passages": passages}


@app.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: str, u: dict = Depends(require_user)):
    try:
        if not rag.delete_document(u["id"], doc_id):
            raise HTTPException(404, "unknown document")
    except HTTPException:
        raise
    except Exception as e:
        raise vector_error(e)
    return {"ok": True}


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
    """Current URL for one picture slot: a users column for avatar and cover, a
    prefs key for the model picture."""
    if kind in ("avatar", "cover"):
        return u.get(kind)
    return (u.get("prefs") or {}).get("model_avatar")


@app.post("/api/auth/media")
async def upload_media(file: UploadFile, kind: str = "avatar", u: dict = Depends(require_user)):
    if kind not in MEDIA_KINDS:
        raise HTTPException(400, "kind must be avatar, cover, or model")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "image required")
    raw = await file.read()
    if len(raw) > 2_000_000:
        raise HTTPException(400, "image too large (max 2MB)")
    ext = "png" if "png" in (file.content_type or "") else "jpg" if "jpeg" in (file.content_type or "") else "webp" if "webp" in (file.content_type or "") else "jpg"
    name = f"{u['id']}-{kind}-{secrets.token_hex(4)}.{ext}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(raw)
    old_url = media_url(u, kind)
    auth.set_media(u["id"], kind, f"/uploads/{name}")
    if old_url and old_url.startswith("/uploads/"):
        (MEDIA_DIR / old_url.split("/")[-1]).unlink(missing_ok=True)
    return {"url": f"/uploads/{name}"}


@app.delete("/api/auth/media")
async def delete_media(kind: str = "avatar", u: dict = Depends(require_user)):
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
async def me(session: str | None = Cookie(None)):
    u = auth.get_session(session)
    if not u:
        raise HTTPException(401, "Login required")
    return u


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not auth.get_session(request.cookies.get("session")):
        return RedirectResponse("/auth.html", 303)
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")