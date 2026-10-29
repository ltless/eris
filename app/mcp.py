"""The Model Context Protocol over Streamable HTTP: one endpoint, JSON-RPC.

The tools are the ones already registered — this exposes them, it does not
define any — so a client sees exactly what a chat turn sees and a tool is added
in one place.
"""

import json
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .deps import require_user
from .security import DESTRUCTIVE, READ_ONLY
from .tools.registry import ToolContext, registry

router = APIRouter()

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
                "readOnlyHint": t.permission == READ_ONLY,
                "destructiveHint": t.permission == DESTRUCTIVE,
                "idempotentHint": t.permission == READ_ONLY,
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
        return urlsplit(origin).netloc.split(":")[0] == (host or "").split(":")[0]
    except Exception:
        return False


@router.post("/mcp")
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


@router.get("/mcp")
async def mcp_get():
    """No server-initiated stream: this server only answers.

    The spec wants either an SSE stream or a 405 here, and a second channel of
    unsolicited events is not what this server has to say.
    """
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
async def mcp_delete(request: Request, u: dict = Depends(require_user)):
    session_id = request.headers.get("mcp-session-id")
    if session_id:
        _mcp_sessions.pop(session_id, None)
    return Response(status_code=204)