"""One HTTP client for the model server, shared by the routes that ask it.

The panel polls the same server every few seconds, and a client per call pays
for a connection each time. A client belongs to the loop that made it, so the
cache is keyed by loop: a test running its own loop gets its own client rather
than one bound to a loop that has already closed.
"""

import asyncio
import weakref

import httpx

_CLIENTS = weakref.WeakKeyDictionary()


def client():
    """The client for the running loop, opened on first use."""
    loop = asyncio.get_running_loop()
    c = _CLIENTS.get(loop)
    if c is None or c.is_closed:
        c = httpx.AsyncClient()
        _CLIENTS[loop] = c
    return c