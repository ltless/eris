"""Safe page fetcher for web_search results.

Search hits come from the open web, so every URL is untrusted input:
scheme/host validation + private-IP blocking before connecting, redirect hops
re-validated one by one, response size capped, text extracted by hand.

Residual risk: DNS rebinding between the getaddrinfo check and the connection.
Acceptable for a single-user self-hosted box; pinning a transport kills it if
this ever faces a hostile network.
"""

import asyncio
import io
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from ..config import settings
from . import as_args

TIMEOUT = 12
MIN_READABLE = 80
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
# A bot UA is refused outright by most CDNs, so this reads as an ordinary
# browser and sends the headers one sends.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
}

_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "form", "nav", "footer", "header")


def _ip_blocked(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        a.is_private
        or a.is_loopback
        or a.is_link_local
        or a.is_reserved
        or a.is_multicast
        or a.is_unspecified
        or (a.version == 6 and a.is_site_local)
    )


def is_public_url(url):
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = p.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return bool(infos) and all(not _ip_blocked(i[4][0]) for i in infos)


def extract_text(raw, max_chars):
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    if soup.head:
        soup.head.decompose()
    best = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\s+", " ", best.get_text(" ")).strip()
    return text[:max_chars] if text else None


async def fetch_page(client, url, max_chars):
    """Return `(text, reason)`. `text` is None when the page could not be read.

    `reason` is a short phrase naming what stopped it, so the caller can say
    which of the several failures happened. Never raises.
    """
    url = str(url)
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
        except ValueError:
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
            return text, ""
        text = extract_text(raw.decode("utf-8", errors="ignore"), max_chars)
        if not text or len(text) < MIN_READABLE:
            return None, "no readable text in the page (scripts, or too little prose)"
        return text, ""
    return None, f"more than {MAX_REDIRECTS} redirects"


async def fetch_pages(urls, max_chars):
    """Fetch several pages concurrently. Returns {url: text_or_None}."""
    urls = list(dict.fromkeys(u for u in urls if u))[:4]
    if not urls:
        return {}
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
        headers=HEADERS,
        limits=httpx.Limits(max_connections=4),
    ) as c:
        texts = await asyncio.gather(*[fetch_page(c, u, max_chars) for u in urls])
    return {u: t for u, (t, _) in zip(urls, texts)}


TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Fetch one URL and return its full readable text. Use it after "
            "web_search to read a specific result in depth, or when the user "
            "gives a link."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to read."},
            },
            "required": ["url"],
        },
    },
}


async def read(args):
    """The page text, or `(False, reason)` for a URL that could not be read."""
    args = as_args(args)
    url = str(args.get("url", "") or "").strip()
    if not url:
        return False, "No URL given."
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=False, headers=HEADERS
    ) as c:
        text, reason = await fetch_page(c, url, settings.fetch_max_chars)
    if not text:
        return False, f"Could not read {url}: {reason}."
    return True, f"Page content for {url}:\n{text}"


async def run(args):
    """Handler for the fetch_url tool. Returns text for the model, never raises."""
    return (await read(args))[1]
