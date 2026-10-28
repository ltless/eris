"""Built-in tools registered against the shared registry.

Each tool module owns its `TOOL` schema and `run` handler. This file adapts
handlers that need run context and wraps results with render text for the model
and source lists for the UI.
"""

from . import calc, fetch, policy, search
from .registry import ToolResult, registry

CHAT_TAGS = ("chat",)


async def _search(args, ctx):
    hits = await search.run(args)
    data = {"hits": hits, "count": len(hits)}
    return ToolResult(
        success=True,
        data=data,
        text=search.render(hits),
        metadata={"sources": _web_sources(hits)},
    )


async def _fetch(args, ctx):
    text = await fetch.run(args)
    url = str((args or {}).get("url", "") or "").strip()
    ok = not text.startswith("Could not read") and not text.startswith("No URL given")
    return ToolResult(
        success=ok,
        data={"url": url},
        text=text,
        error=None if ok else {"code": "tool_error", "message": text, "retryable": False},
    )


async def _calc(args, ctx):
    text = await calc.run(args)
    ok = not text.startswith("calc:")
    return ToolResult(
        success=ok,
        data={"expression": str((args or {}).get("expression", "") or "")},
        text=text,
        error=None if ok else {"code": "invalid_arguments", "message": text, "retryable": False},
    )


def _web_sources(hits):
    return [
        {
            "source": h.get("url") or h.get("title") or "",
            "title": h.get("title") or h.get("url") or "",
            "kind": "web",
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
            permission=policy.EXTERNAL,
            timeout=45.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            fetch.TOOL,
            handler=_fetch,
            permission=policy.EXTERNAL,
            timeout=30.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="network_read",
        )
    )
    registry.add(
        _tool(
            calc.TOOL,
            handler=_calc,
            permission=policy.READ_ONLY,
            timeout=10.0,
            tags=CHAT_TAGS + ("research",),
            side_effect="none",
        )
    )


def _tool(module_tool, *, handler, permission, timeout, tags, side_effect):
    from .registry import Tool

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
    )
