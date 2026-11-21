"""The built-in tools, registered against the shared registry.

Each tool module declares its own `Tool` — name, schema, permission, timeout
and tags — so a change to one tool lands in that tool's file alone. This module
only decides which of them a chat run gets.
"""

from . import (
    crypto,
    equity,
    fetch,
    files,
    market,
    plan,
    search,
    wayback,
    weather,
    write,
)
from .registry import registry

MODULES = (
    search, fetch, weather, wayback, crypto, market, equity, plan, files, write,
)


def register_builtins():
    """Register the built-in tools. Idempotent, so importing twice is safe."""
    if search.NAME in registry.names():
        return
    for module in MODULES:
        module.register()