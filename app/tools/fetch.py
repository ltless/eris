"""Safe page fetcher for web_search results.

Search hits come from the open web, so every URL is untrusted input: scheme and
host validation, private-IP blocking and redirect re-validation all live in
`..net`, and this module only reads what that allows.
"""

import asyncio
import io
import re
import time

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from ..config import settings
from ..errors import TOOL_ERROR
from ..net import HEADERS, is_public_url
from ..security import EXTERNAL
from .registry import TAGS, Tool, ToolResult, missing_arg, registry

TIMEOUT = 12
MIN_READABLE = 80
MAX_BYTES = 2_000_000
# A page's own redirect chain is worth following: it is how a link that moved
# still answers. Five hops is a chain, not a chase.
MAX_REDIRECTS = 5
# Concurrent page reads in one `fetch_pages` call.
MAX_PAGES = 4
NAME = "fetch_url"
TIMEOUT_S = 30.0

_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "form", "nav", "footer", "header")

# Pages come back unchanged for minutes at a time, and one search round asks
# for the same top results again when the model repeats a query. Only successes
# are cached: a failure is often the site being briefly down, and the next
# attempt should go to the network.
TTL = 900
MAX_CACHED = 64
_cache: dict[tuple[str, int], tuple[float, str]] = {}

DESCRIPTION = (
    "Fetch one URL and return its full readable text. Use it after "
    "web_search to read a specific result in depth, or when the user "
    "gives a link."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "The URL to read."},
    },
    "required": ["url"],
}


def _cached(url, max_chars):
    hit = _cache.get((url, max_chars))
    if hit is None:
        return None
    expires, text = hit
    if time.monotonic() >= expires:
        _cache.pop((url, max_chars), None)
        return None
    return text


def _remember(url, max_chars, text):
    if len(_cache) >= MAX_CACHED:
        _cache.clear()
    _cache[(url, max_chars)] = (time.monotonic() + TTL, text)


def extract_text(raw, max_chars):
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    if soup.head:
        soup.head.decompose()
    # The first container that exists is not the one holding the prose: a site
    # can ship an empty <main> and put the article elsewhere, and picking by
    # presence then returned a few words and called the page unreadable. The
    # fullest container wins instead.
    candidates = [c for c in (soup.find("main"), soup.find("article"), soup.body, soup)
                  if c is not None]
    texts = [re.sub(r"\s+", " ", c.get_text(" ")).strip() for c in candidates]
    text = max(texts, key=len) if texts else ""
    return text[:max_chars] if text else None


async def fetch_page(client, url, max_chars):
    """Return `(text, reason)`. `text` is None when the page could not be read;
    `reason` names which failure stopped it. Never raises."""
    url = str(url)
    hit = _cached(url, max_chars)
    if hit is not None:
        return hit, ""
    asked = url
    for _ in range(MAX_REDIRECTS + 1):
        if not await asyncio.to_thread(is_public_url, url):
            return None, "not a public http(s) address"
        try:
            async with client.stream("GET", url, follow_redirects=False) as r:
                if 300 <= r.status_code < 400:
                    loc = r.headers.get("location")
                    if not loc:
                        return None, f"redirect from {url} without a destination"
                    url = str(httpx.URL(str(r.url)).join(loc))
                    continue
                if r.status_code >= 400:
                    return None, f"the server answered {r.status_code}"
                ctype = (r.headers.get("content-type") or "").lower()
                if "html" not in ctype and "text" not in ctype and "pdf" not in ctype:
                    return None, f"not a readable document ({ctype or 'no content-type'})"
                total = 0
                chunks = []
                async for part in r.aiter_bytes():
                    total += len(part)
                    chunks.append(part)
                    if total >= MAX_BYTES:
                        break
        except httpx.HTTPError as e:
            return None, f"the request failed ({type(e).__name__})"
        except (httpx.InvalidURL, ValueError):
            # `InvalidURL` descends from neither `HTTPError` nor `ValueError`,
            # and a port or host that will not parse reaches it: the address
            # guard asks the resolver, which ignores the port.
            return None, "the URL could not be followed"
        raw = b"".join(chunks)
        if "pdf" in ctype:
            try:
                text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
                text = re.sub(r"\s+", " ", text).strip()[:max_chars]
            except Exception:
                return None, "the PDF could not be parsed"
            if not text or len(text) < MIN_READABLE:
                return None, "the PDF held no readable text"
            _remember(asked, max_chars, text)
            return text, ""
        text = extract_text(raw.decode("utf-8", errors="ignore"), max_chars)
        if not text or len(text) < MIN_READABLE:
            return None, "no readable text in the page (scripts, or too little prose)"
        _remember(asked, max_chars, text)
        return text, ""
    return None, f"more than {MAX_REDIRECTS} redirects"


async def fetch_pages(urls, max_chars):
    """Fetch several pages concurrently. Returns {url: text_or_None}.

    How many is the caller's call: `search` passes `settings.fetch_top`, and a
    cap here as well would drop a page after the caller had already counted it.
    `MAX_PAGES` is the connection pool this shares, not a second limit.
    """
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        return {}
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
        headers=HEADERS,
        limits=httpx.Limits(max_connections=MAX_PAGES),
    ) as c:
        texts = await asyncio.gather(*[fetch_page(c, u, max_chars) for u in urls])
    return {u: t for u, (t, _) in zip(urls, texts)}


async def read(args):
    """Answer the fetch tool, or say which failure stopped it."""
    url = str(args.get("url", "") or "").strip()
    if not url:
        return missing_arg("URL")
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=False, headers=HEADERS
    ) as c:
        text, reason = await fetch_page(c, url, settings.fetch_max_chars)
    if not text:
        return ToolResult.failed(TOOL_ERROR, f"Could not read {url}: {reason}.", reason=reason)
    return ToolResult.ok(f"Page content for {url}:\n{text}")


def register():
    return registry.add(Tool(
        name=NAME,
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=read,
        permission=EXTERNAL,
        timeout=TIMEOUT_S,
        tags=TAGS,
    ))
