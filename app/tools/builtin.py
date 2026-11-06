"""Built-in tools registered against the shared registry.

Each tool module owns its `TOOL` schema and `run` handler. This file adapts
handlers that need run context and wraps results with render text for the model
and source lists for the UI.
"""

from . import crypto, equity, fetch, files, market, search, wayback, weather
from ..security import EXTERNAL, READ_ONLY
from ..runtime.errors import TOOL_ERROR
from .registry import Tool, ToolResult, registry

CHAT_TAGS = ("chat",)


async def _search(args, ctx):
    hits, reason = await search.run_checked(args)
    # A search that never reached the server is a failed call, not an empty
    # result set: the model has to be told the difference or it answers from
    # memory on the strength of "nothing was found". The text carries that
    # sentence, because a failed result with no text reaches the model as
    # "(no result)" and it learns nothing.
    if reason:
        message = f"Web search unavailable ({reason}). Tell the user the search did not run."
        return ToolResult(
            success=False,
            error={"code": TOOL_ERROR, "message": message, "retryable": True},
            text=message,
        )
    data = {"hits": hits, "count": len(hits)}
    return ToolResult(
        success=True,
        data=data,
        text=search.render(hits),
        metadata={"sources": _web_sources(hits)},
    )


def _has_files(ctx):
    return bool(getattr(ctx, "files", ()))


async def _read_file(args, ctx):
    ok, text, reason = files.read(args, ctx)
    return ToolResult(
        success=ok,
        data={"reason": reason},
        text=text,
        metadata={"reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


async def _fetch(args, ctx):
    ok, text, reason = await fetch.read(args)
    url = str((args or {}).get("url", "") or "").strip()
    return ToolResult(
        success=ok,
        data={"url": url},
        text=text,
        # `reason` is what the card shows on a replay, where the streamed frames
        # are gone and all that is left is this row. The text keeps it for the
        # model; the metadata keeps it for the reader.
        metadata={"reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


async def _weather(args, ctx):
    ok, text, reason, meta = await weather.read(args)
    return ToolResult(
        success=ok,
        data={"reason": reason},
        text=text,
        metadata={**meta, "reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


async def _wayback(args, ctx):
    ok, text, reason, meta = await wayback.read(args)
    return ToolResult(
        success=ok,
        data={"reason": reason},
        text=text,
        metadata={**meta, "reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


async def _crypto(args, ctx):
    ok, text, reason, meta = await crypto.read(args)
    return ToolResult(
        success=ok,
        data={"reason": reason},
        text=text,
        metadata={**meta, "reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


async def _trend(args, ctx):
    ok, text, reason, meta = await market.read_trend(args)
    return ToolResult(
        success=ok,
        data={"reason": reason},
        text=text,
        metadata={**meta, "reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


async def _equity(args, ctx):
    ok, text, reason, meta = await equity.read(args)
    return ToolResult(
        success=ok,
        data={"reason": reason},
        text=text,
        metadata={**meta, "reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


async def _equity_trend(args, ctx):
    ok, text, reason, meta = await equity.read_trend(args)
    return ToolResult(
        success=ok,
        data={"reason": reason},
        text=text,
        metadata={**meta, "reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


async def _screen(args, ctx):
    ok, text, reason, meta = await equity.read_screen(args)
    return ToolResult(
        success=ok,
        data={"reason": reason},
        text=text,
        metadata={**meta, "reason": reason},
        error=None if ok else {"code": TOOL_ERROR, "message": text, "retryable": False},
    )


def _web_sources(hits):
    return [
        {
            "source": h.get("url") or h.get("title") or "",
            "title": h.get("title") or h.get("url") or "",
            "kind": "web",
            # Whether this page's text was actually read. The text itself goes
            # to the model and never to the browser, so this flag is the only
            # thing that lets the card tell a page the answer may rest on from
            # one that was never more than a snippet.
            "read": bool(h.get("page")),
        }
        for h in hits
        if h.get("url") or h.get("title")
    ]


def register_builtins():
    """Register the built-in tools. Idempotent, so importing twice is safe."""
    if "web_search" in registry.names():
        return
    registry.add(
        _tool(
            search.TOOL,
            handler=_search,
            permission=EXTERNAL,
            timeout=45.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            fetch.TOOL,
            handler=_fetch,
            permission=EXTERNAL,
            timeout=30.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            weather.TOOL,
            handler=_weather,
            permission=EXTERNAL,
            timeout=25.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            wayback.TOOL,
            handler=_wayback,
            permission=EXTERNAL,
            timeout=40.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            crypto.TOOL,
            handler=_crypto,
            permission=EXTERNAL,
            timeout=35.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            market.TREND_TOOL,
            handler=_trend,
            permission=EXTERNAL,
            timeout=35.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            equity.TOOL,
            handler=_equity,
            permission=EXTERNAL,
            timeout=35.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            equity.TREND_TOOL,
            handler=_equity_trend,
            permission=EXTERNAL,
            timeout=40.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            equity.SCREEN_TOOL,
            handler=_screen,
            permission=EXTERNAL,
            timeout=35.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            files.TOOL,
            handler=_read_file,
            permission=READ_ONLY,
            timeout=10.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="none",
            when=_has_files,
        )
    )


def _tool(module_tool, *, handler, permission, timeout, tags, side_effect, when=None):
    spec = module_tool["function"]
    return Tool(
        name=spec["name"],
        description=spec["description"],
        parameters=spec["parameters"],
        handler=handler,
        permission=permission,
        timeout=timeout,
        tags=tags,
        side_effect=side_effect,
        when=when,
    )
