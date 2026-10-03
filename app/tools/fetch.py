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
import json
import re
import socket
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from ..config import settings

TIMEOUT = 12
MIN_READABLE = 200
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; ErisBot/1.0)"

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
    """Return up to max_chars of readable page text, or None. Never raises."""
    url = str(url)
    for _ in range(MAX_REDIRECTS + 1):
        if not await asyncio.to_thread(is_public_url, url):
            return None
        try:
            async with client.stream("GET", url, follow_redirects=False) as r:
                if 300 <= r.status_code < 400:
                    loc = r.headers.get("location")
                    if not loc:
                        return None
                    url = str(httpx.URL(str(r.url)).join(loc))
                    continue
                if r.status_code >= 400:
                    return None
                ctype = (r.headers.get("content-type") or "").lower()
                if "html" not in ctype and "text" not in ctype and "pdf" not in ctype:
                    return None
                total = 0
                chunks = []
                async for part in r.aiter_bytes():
                    total += len(part)
                    chunks.append(part)
                    if total >= MAX_BYTES:
                        break
        except (httpx.HTTPError, ValueError):
            return None
        raw = b"".join(chunks)
        if "pdf" in ctype:
            try:
                text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
            except Exception:
                return None
        else:
            text = extract_text(raw.decode("utf-8", errors="ignore"), max_chars)
        return text if text and len(text) >= MIN_READABLE else None
    return None


async def fetch_pages(urls, max_chars):
    """Fetch several pages concurrently. Returns {url: text_or_None}."""
    urls = list(dict.fromkeys(u for u in urls if u))[:4]
    if not urls:
        return {}
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=4),
    ) as c:
        texts = await asyncio.gather(*[fetch_page(c, u, max_chars) for u in urls])
    return dict(zip(urls, texts))


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


async def run(args):
    """Handler for the fetch_url tool. Returns text for the model, never raises."""
    if not isinstance(args, dict):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}
    url = str(args.get("url", "") or "").strip()
    if not url:
        return "No URL given."
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT}
    ) as c:
        text = await fetch_page(c, url, settings.fetch_max_chars)
    if not text:
        return f"Could not read {url} (non-public address, offline, or not readable text)."
    return f"Page content for {url}:\n{text}"
