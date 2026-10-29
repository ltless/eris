"""Runtime events, in the frames the browser already parses.

The names differ between the two eras of this app — `tool.started` here,
`tool` there — and the frontend is not being rewritten, so the mapping lives
here, in one place, with the event still carrying its own fields.
"""

import json

from .runtime import events as ev


def sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def sse_frame(event):
    """One runtime event as a frame dict, or `None` when the browser has no
    use for it."""
    d = event.data or {}
    t = event.type
    if t == ev.MODEL_DELTA:
        return {"type": "delta", "text": d.get("text", "")}
    if t == ev.MODEL_THINKING:
        return {"type": "thinking", "text": d.get("text", "")}
    if t == ev.SOURCE_FOUND:
        # `r` rides along so the browser can file a result under the round that
        # found it without guessing from arrival order, which is what lets a
        # round's searches and its fetches be written back as one record.
        return {"type": "sources", "r": d.get("round", 0), "items": [{
            "n": d.get("n"), "source": d.get("source", ""), "title": d.get("title", ""),
            "kind": d.get("kind", "web"), "read": bool(d.get("read")),
        }]}
    if t == ev.TOOL_STARTED and d.get("tool") == "web_search":
        return {"type": "tool", "r": d.get("round", 0),
                "query": str((d.get("args") or {}).get("query", "") or "")}
    # A fetch is its own step, not part of the search card: it is the model
    # going back for one page in depth, and it has a URL the search never
    # showed. It is announced when it starts, because the point of drawing it
    # is that the user can watch a slow page being read, and closed when the
    # tool ends rather than when the run does.
    if t == ev.TOOL_STARTED and d.get("tool") == "fetch_url":
        return {"type": "fetch", "r": d.get("round", 0),
                "url": str((d.get("args") or {}).get("url", "") or "")}
    if t == ev.TOOL_COMPLETED and d.get("tool") == "fetch_url":
        return {"type": "fetch_done", "r": d.get("round", 0),
                "ok": True, "chars": d.get("chars", 0)}
    if t == ev.TOOL_FAILED:
        return {"type": "tool_error", "tool": d.get("tool", ""), "r": d.get("round", 0),
                "code": d.get("code", ""), "message": d.get("message", "")}
    if t == ev.MODEL_METRICS:
        return {"type": "metrics", **{k: v for k, v in d.items() if k != "round"}}
    if t == ev.APPROVAL_REQUIRED:
        return {"type": "approval", "tool": d.get("tool", ""), "message": d.get("message", "")}
    if t in (ev.TASK_FAILED, ev.TASK_CANCELLED):
        return {"type": "error", "message": d.get("message") or d.get("reason") or t}
    return None