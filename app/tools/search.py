import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunsplit

import httpx

from ..config import settings
from . import as_args, fetch

TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web in English. Always translate the query to English "
            "first, whatever language the user wrote in. Use it for current "
            "facts: news, prices, versions, releases, or anything that may "
            "have changed recently. Keep the query short, three to six words, "
            "like a person typing into a search box: the thing, then the "
            "place. Leave out dates and the user's whole sentence; the "
            "time_range argument covers recency. If one site holds the answer, "
            "name it, such as BMKG for Indonesian weather or a vendor's own "
            "site for a version number."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Short English search query, no date.",
                },
                # Both are free strings on purpose. A small model writes
                # "generalnews" or "\"day\"", and an enum turns that slip into a
                # refused call instead of a search with the argument dropped.
                "time_range": {
                    "type": "string",
                    "description": (
                        "Only recent results: one of day, week, month, year. Use "
                        "day or week for prices and news, month or year for "
                        "releases and versions."
                    ),
                },
                "categories": {
                    "type": "string",
                    "description": "One of general, news. Use news for current events.",
                },
            },
            "required": ["query"],
        },
    },
}

LOW_VALUE = ("facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "youtube.com")
TRUSTED_SUFFIXES = (".gov", ".edu")
TRUSTED_DOMAINS = ("wikipedia.org", "arxiv.org", "github.com")

# A fresh query scores a result published inside RECENT_DAYS at full recency and
# one at STALE_DAYS or older at none, sliding in between.
RECENT_DAYS = 7
STALE_DAYS = 30


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


DATE_RE = re.compile(
    r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b"  # 2026-10-19, 2026.10.19
    r"|\b\d{1,2}[/.]\d{1,2}[/.](?:19|20)\d{2}\b"  # 19/10/2026
)


def clean_query(query):
    """Drop date-shaped tokens the model pasted into the query.

    The engine treats them as a filter it cannot satisfy. The same Bali weather
    query returned 5 travel listings with 2026-10-19 in it and 20 on-topic
    results without, and recency is already the time_range argument's job. A
    query that is nothing but a date is left alone rather than emptied.
    """
    trimmed = re.sub(r"\s+", " ", DATE_RE.sub(" ", query)).strip()
    return trimmed or query


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
            # Copy: callers keep their own hit dicts untouched.
            uniq.append({**r, "url": key})
    scored = []
    for i, r in enumerate(uniq):
        title = (r.get("title") or "").lower()
        host = urlparse(r["url"]).netloc
        title_hits = sum(1 for t in terms if _word_in(title, t))
        domain = 1.5 if host.endswith(TRUSTED_SUFFIXES) or host in TRUSTED_DOMAINS else 0.0
        penalty = 2.0 if any(host == d or host.endswith(f".{d}") for d in LOW_VALUE) else 0.0
        recency = 0.0
        if fresh:
            age = _age_days(r.get("published"))
            if age is not None:
                # Full credit inside the week, none past the month, linear between.
                span = STALE_DAYS - RECENT_DAYS
                recency = max(0.0, min(1.0, (STALE_DAYS - age) / span))
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


def render(results, start=0):
    if not results:
        return (
            "No web results were found for this query. Tell the user you "
            "found nothing relevant. Do NOT answer from memory."
        )
    head = f"Web results (as of {date.today().isoformat()}):\n\n"
    parts = []
    for i, r in enumerate(results):
        block = f"[{start + i + 1}] {r['title']} — {r['url']}\n{r['snippet']}"
        if r.get("page"):
            block += f"\nPage content: {r['page']}"
        elif "page" in r:
            block += "\n(the full page could not be read — snippet only)"
        parts.append(block)
    return head + "\n\n".join(parts)


def _pick(value, allowed):
    """The one allowed word a small model meant.

    It writes "\\"day\\"" with the quotes still on it, or runs two options
    together as "generalnews". An exact miss is not a reason to fail the call:
    the last allowed word found inside the text is the one it reached for, and
    nothing found leaves the parameter off entirely.
    """
    text = str(value or "").strip().strip("\"'`").strip().lower()
    if text in allowed:
        return text
    return max((a for a in allowed if a in text), key=text.rfind, default="")


def time_range_for(args, query):
    tr = _pick(args.get("time_range"), ("day", "week", "month", "year"))
    if tr:
        return tr
    if FRESH_RE.search(query):
        return "week"
    return settings.searxng_time_range


def _down_engines(data):
    """Engines SearXNG could not reach on this search.

    A suspended engine is still a 200 with whatever the rest produced, so this
    is only an explanation when the result set is empty; naming the engines is
    what turns "found nothing" into "the search did not run".
    """
    out = []
    for item in data.get("unresponsive_engines") or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (list, tuple)) and item:
            out.append(str(item[0]))
        elif isinstance(item, dict) and item.get("engine"):
            out.append(str(item["engine"]))
    return out


async def _query(client, params):
    r = await client.get(f"{settings.searxng_url}/search", params=params)
    r.raise_for_status()
    return r.json()


def _hits(data):
    return [
        {
            "title": x.get("title") or x.get("url") or "",
            "url": x.get("url") or "",
            "snippet": (x.get("content") or "").strip(),
            "published": x.get("publishedDate") or "",
        }
        for x in (data.get("results") or [])[: max(settings.searxng_top_k * 4, 15)]
        if x.get("url")
    ]


async def run_checked(args):
    """`(hits, reason)`. `reason` is empty unless the search itself failed.

    A dead server and a query with no results both come back as no hits, so
    the failure has to be reported apart from them or a broken SearXNG reads
    as a successful search that found nothing. A stack whose engines are all
    suspended answers 200 with no rows at all, and reading that as "nothing
    found" is what made the search look useless while it was being rate
    limited. Only an empty answer after the filters are dropped was asked of
    every engine, so that is the one whose suspended list explains anything:
    the engines named under `news` are news engines, not the whole stack.
    """
    args = as_args(args)
    query = clean_query(str(args.get("query", "")).strip())
    if not query:
        return [], ""
    tr = time_range_for(args, query)
    params = {
        "q": query,
        "format": "json",
        "language": settings.searxng_language,
    }
    cat = _pick(args.get("categories"), ("general", "news"))
    if cat:
        params["categories"] = cat
    if tr:
        params["time_range"] = tr
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            data = await _query(c, params)
            hits = _hits(data)
            # A model that asked for news within a day gets a much thinner
            # index than the one it could have had, and that pair is what most
            # often came back with nothing. Dropping the filters costs one more
            # request, and only when the filtered one was empty, so it is always
            # worth trying: the engines named as unresponsive for `news` are the
            # news ones, and the general stack behind them still answers.
            if not hits and (cat or tr):
                wider = {k: v for k, v in params.items() if k not in ("categories", "time_range")}
                data = await _query(c, wider)
                hits = _hits(data)
    except Exception as e:
        return [], f"search failed: {type(e).__name__}"
    if not hits:
        down = _down_engines(data)
        if down:
            # Every engine suspended still answers 200 with an empty list. That
            # is not a search that found nothing, and saying so would have the
            # model write a confident answer to a question nobody searched.
            return [], f"search engines unavailable ({', '.join(down[:3])})"
    fresh = bool(tr) or bool(FRESH_RE.search(query))
    hits = rank(query, hits, fresh=fresh)[: settings.searxng_top_k]
    if settings.fetch_pages and hits:
        pages = await fetch.fetch_pages(
            [h["url"] for h in hits[: settings.fetch_top]], settings.fetch_max_chars
        )
        for h in hits[: settings.fetch_top]:
            h["page"] = pages.get(h["url"])
    return hits, ""


async def run(args):
    return (await run_checked(args))[0]
