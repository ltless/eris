import json
import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunsplit

import httpx

from ..config import settings
from . import fetch

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
                "categories": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "Use news for current-events queries.",
                },
            },
            "required": ["query"],
        },
    },
}

LOW_VALUE = ("facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "youtube.com")
TRUSTED_SUFFIXES = (".gov", ".edu")
TRUSTED_DOMAINS = ("wikipedia.org", "arxiv.org", "github.com")


def _word_in(text, term):
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def _clean_url(url):
    p = urlparse(url)
    keep = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_") and k != "ref"]
    q = urlencode(keep)
    path = p.path.rstrip("/")
    return urlunsplit((p.scheme, p.netloc.lower(), path or "/", q, ""))


def _age_days(published):
    if not published:
        return None
    try:
        d = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(d.tzinfo) - d).days


def rank(query, results, fresh=False):
    """Relevance-first ordering, then URL dedup and max 2 results per domain.

    Pure function. `fresh` adds a recency term (<=7d scores 1, >=30d scores 0)
    for queries that are about current events.
    """
    terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
    seen = set()
    uniq = []
    for r in results:
        key = _clean_url(r.get("url") or "")
        if key and key not in seen:
            seen.add(key)
            r["url"] = key
            uniq.append(r)
    scored = []
    for i, r in enumerate(uniq):
        title = (r.get("title") or "").lower()
        host = urlparse(r["url"]).netloc
        title_hits = sum(1 for t in terms if _word_in(title, t))
        domain = 1.5 if host.endswith(TRUSTED_SUFFIXES) or host in TRUSTED_DOMAINS else 0.0
        penalty = 2.0 if any(host.endswith(d) or f".{d}" in f".{host}" for d in LOW_VALUE) else 0.0
        recency = 0.0
        if fresh:
            age = _age_days(r.get("published"))
            recency = 1.0 if age is not None and age <= 7 else (0.0 if age is None or age >= 30 else (30 - age) / 23)
        scored.append((title_hits + domain - penalty + recency, -i, r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    per_host, out = {}, []
    for _, _, r in scored:
        host = urlparse(r["url"]).netloc
        if per_host.get(host, 0) >= 2:
            continue
        per_host[host] = per_host.get(host, 0) + 1
        out.append(r)
    return out


FRESH_RE = re.compile(
    r"\b(latest|newest|today|now|current|recent|berita|terbaru|hari ini|"
    r"sekarang|harga|price)\b",
    re.I,
)


def render(results):
    if not results:
        return (
            "No web results were found for this query. Tell the user you "
            "found nothing relevant. Do NOT answer from memory."
        )
    head = f"Web results (as of {date.today().isoformat()}):\n\n"
    parts = []
    for i, r in enumerate(results):
        block = f"[{i + 1}] {r['title']} — {r['url']}\n{r['snippet']}"
        if r.get("page"):
            block += f"\nPage content: {r['page']}"
        parts.append(block)
    return head + "\n\n".join(parts)


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
    tr = time_range_for(args, query)
    params = {
        "q": query,
        "format": "json",
        "language": settings.searxng_language,
    }
    cat = str(args.get("categories", "") or "")
    if cat in ("general", "news"):
        params["categories"] = cat
    if tr:
        params["time_range"] = tr
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{settings.searxng_url}/search", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return []
    hits = [
        {
            "title": x.get("title") or x.get("url") or "",
            "url": x.get("url") or "",
            "snippet": (x.get("content") or "").strip(),
            "published": x.get("publishedDate") or "",
        }
        for x in (data.get("results") or [])[: max(settings.searxng_top_k * 4, 15)]
        if x.get("url")
    ]
    fresh = bool(tr) or bool(FRESH_RE.search(query))
    hits = rank(query, hits, fresh=fresh)[: settings.searxng_top_k]
    if settings.fetch_pages and hits:
        pages = await fetch.fetch_pages(
            [h["url"] for h in hits[: settings.fetch_top]], settings.fetch_max_chars
        )
        for h in hits:
            h["page"] = pages.get(h["url"])
    return hits
