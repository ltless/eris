"""Talking to addresses the user's machine did not choose.

Search hits, links inside a page and favicon domains all arrive as untrusted
input, so a URL is checked before anything connects to it: scheme and host
first, then every address that host resolves to. The page fetcher and the icon
fetcher both start here, so neither is the one that forgot to check.

Residual risk: DNS rebinding between the getaddrinfo check and the connection.
Acceptable for a single-user self-hosted box; pinning a transport kills it if
this ever faces a hostile network.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

# A bot UA is refused outright by most CDNs, so this reads as an ordinary
# browser and sends the headers one sends. Sec-Fetch-* and the referer are what
# most 403s actually check: without them a page that opens in a browser answers
# 403 to the same URL here (medium, weather-atlas). A site that still refuses is
# refusing the request itself, not its shape, and no header fixes that.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Referer": "https://www.google.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Upgrade-Insecure-Requests": "1",
}


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
    """True when every address `url`'s host resolves to is on the open internet.

    Resolving blocks, so callers run this off the event loop.
    """
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
