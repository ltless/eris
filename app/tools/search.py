import json
import re
from datetime import date

import httpx

from ..config import settings

TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web in English. Always translate the query to English "
            "first, whatever language the user wrote in. Use it for current "
            "facts: news, prices, versions, releases, or anything that may "
            "have changed recently."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "English search query.",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": (
                        "Only recent results. Use day or week for prices and "
                        "news, month or year for releases and versions."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

FRESH_RE = re.compile(
    r"\b(latest|newest|today|now|current|recent|berita|terbaru|hari ini|"
    r"sekarang|harga|price)\b",
    re.I,
)


def render(results):
    if not results:
        return "No results."
    head = f"Web results (as of {date.today().isoformat()}):\n\n"
    return head + "\n\n".join(
        f"[{i + 1}] {r['title']} — {r['url']}\n{r['snippet']}" for i, r in enumerate(results)
    )


def time_range_for(args, query):
    tr = str(args.get("time_range", "") or "")
    if tr in ("day", "week", "month", "year"):
        return tr
    if FRESH_RE.search(query):
        return "week"
    return settings.searxng_time_range


async def run(args):
    if not isinstance(args, dict):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}
    query = str(args.get("query", "")).strip()
    if not query:
        return []
    params = {
        "q": query,
        "format": "json",
        "language": settings.searxng_language,
    }
    tr = time_range_for(args, query)
    if tr:
        params["time_range"] = tr
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{settings.searxng_url}/search", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return []
    return [
        {
            "title": x.get("title") or x.get("url") or "",
            "url": x.get("url") or "",
            "snippet": (x.get("content") or "").strip(),
        }
        for x in (data.get("results") or [])[: settings.searxng_top_k]
        if x.get("url")
    ]
