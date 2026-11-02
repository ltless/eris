"""Site icons for search results, read from the site itself.

A remote favicon service would hand every result domain to a third party,
which is what the self-hosted search stack exists to avoid, so the icon is
requested from the domain directly and kept for a day. Nothing here is load
bearing: a site with no icon leaves the browser with the letter chip, which is
what it showed before.
"""

import asyncio
import re
import time

import httpx
from bs4 import BeautifulSoup

from .tools.fetch import HEADERS, is_public_url

DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
ICON_TYPES = (
    "image/x-icon",
    "image/vnd.microsoft.icon",
    "image/png",
    "image/gif",
    "image/jpeg",
    "image/webp",
    "image/svg+xml",
    "image/avif",
)
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_BYTES = 64_000
MAX_HTML = 256_000
MAX_REDIRECTS = 3
# A page can name half a dozen icons; the first few are the one it means.
MAX_CANDIDATES = 4
TIMEOUT = 6
TTL_HIT = 24 * 3600
TTL_MISS = 3600
MAX_CACHED = 512

# `(expires_at, icon_or_None)`, keyed by domain. A miss is cached too: a site
# with no icon is asked once, not on every row that points at it.
_cache: dict[str, tuple[float, tuple[bytes, str] | None]] = {}


async def icon_for(domain):
    """`(bytes, content_type)` for the domain's icon, or None."""
    domain = str(domain or "").strip().lower().rstrip(".")
    if not DOMAIN_RE.match(domain) or ".." in domain:
        return None
    now = time.monotonic()
    cached = _cache.get(domain)
    if cached and cached[0] > now:
        return cached[1]
    icon = await _download(domain)
    if len(_cache) >= MAX_CACHED:
        _cache.clear()
    _cache[domain] = (now + (TTL_HIT if icon else TTL_MISS), icon)
    return icon


async def _download(domain):
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
        icon = await _fetch(c, f"https://{domain}/favicon.ico", MAX_BYTES, ICON_TYPES)
        if icon:
            return icon[0], icon[1]
        # Plenty of sites serve no icon at that path and name the real one in
        # the page instead, so the miss is followed up rather than accepted.
        page = await _fetch(c, f"https://{domain}/", MAX_HTML, HTML_TYPES)
        if not page:
            return None
        _raw, _ctype, landed = page
        # A bare host often redirects to its real home on another origin, and
        # the declared links belong to that one rather than to the host asked.
        origin = str(httpx.URL(landed).join("/"))
        if origin != f"https://{domain}/":
            icon = await _fetch(c, origin + "favicon.ico", MAX_BYTES, ICON_TYPES)
            if icon:
                return icon[0], icon[1]
        for url in _declared(_raw, landed)[:MAX_CANDIDATES]:
            icon = await _fetch(c, url, MAX_BYTES, ICON_TYPES)
            if icon:
                return icon[0], icon[1]
    return None


async def _fetch(client, url, max_bytes, types):
    """`(bytes, content_type, final_url)` for a URL of one of `types`, or None.

    A redirect is followed by hand so the guard can run on every hop: a public
    host is free to point at one that is not, and following that blindly is the
    whole SSRF hole. The URL that finally answered comes back with the body,
    because relative links in it resolve against that one, not the first.
    """
    for _ in range(MAX_REDIRECTS + 1):
        if not await asyncio.to_thread(is_public_url, url):
            return None
        try:
            async with client.stream("GET", url) as r:
                if 300 <= r.status_code < 400:
                    loc = r.headers.get("location")
                    if not loc:
                        return None
                    url = str(httpx.URL(url).join(loc))
                    continue
                if r.status_code >= 400:
                    return None
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype not in types:
                    return None
                chunks, total = [], 0
                async for part in r.aiter_bytes():
                    total += len(part)
                    chunks.append(part)
                    if total >= max_bytes:
                        break
        except (httpx.HTTPError, ValueError):
            return None
        raw = b"".join(chunks)[:max_bytes]
        return (raw, ctype, url) if raw else None
    return None


def _declared(html, base):
    """The icon URLs a page names, resolved against it, in the order it lists them.

    `rel` carries several tokens (`shortcut icon`), so it is matched as a whole
    rather than compared. Anything that is not http is dropped: these URLs go
    straight into a request.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        if "icon" not in rel:
            continue
        href = (link.get("href") or "").strip()
        if not href or href.startswith("data:"):
            continue
        try:
            url = str(httpx.URL(base).join(href))
        except (httpx.InvalidURL, ValueError):
            continue
        if url.startswith(("http://", "https://")) and url not in out:
            out.append(url)
    return out