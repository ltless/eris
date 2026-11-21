"""Typed tools, one registry.

Every capability is declared here: name, JSON schema, permissions, and timeout.
The runtime looks tools up by name; adding a tool is registration, not an
edit to the loop.
"""

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Callable

import jsonschema

from ..errors import (
    INVALID_ARGUMENTS,
    TIMEOUT,
    TOOL_ERROR,
    UNKNOWN_TOOL,
    ToolError,
)
from ..security import READ_ONLY, authorize
from ..security.workspace import Access


# What every tool in this app is. A tuple rather than a string because
# `specs_for` reads it as a set of tags; every caller passes the one tag a chat
# run uses.
TAGS = ("chat",)


@dataclass
class SourceNumbers:
    """The numbers a run hands out to its sources.

    A tool that returns several results reserves a block, so the numbers it
    writes into the text the model reads are the same ones the browser links a
    citation to. Without that, a second search in one turn restarts at 1 and
    `[1]` can point at either search's first hit.
    """

    next: int = 1

    def reserve(self, count=1):
        start = self.next
        self.next += count
        return start


@dataclass
class ToolContext:
    """What a run knows that its tools may need, passed to every call."""

    # Text of the files attached to this run. A long attachment is read through
    # a tool rather than pasted into the prompt, where every round would pay
    # for it again.
    files: tuple = ()
    # The steps the run wrote down for itself. It lives on the context, not in
    # the conversation, so the engine can restate it after history is pruned.
    plan: tuple = ()
    # Shared by the tools and the engine, which is the point: whoever files a
    # source takes the next number from here rather than counting its own.
    numbers: SourceNumbers = field(default_factory=SourceNumbers)
    workspace: Access | None = None


@dataclass(frozen=True)
class ToolResult:
    """What a handler returns. `text` is what the model reads, and `metadata`
    carries what the run and the browser need but the model does not: `sources`
    for the cards' source list, `card` for a tool's own figures, and `reason`
    for the short phrase a replay shows."""

    success: bool = True
    error: dict | None = None
    metadata: dict = field(default_factory=dict)
    text: str | None = None

    @classmethod
    def ok(cls, text=None, *, sources=(), card=None):
        meta = {}
        if sources:
            meta["sources"] = list(sources)
        if card is not None:
            meta["card"] = card
        return cls(success=True, text=text, metadata=meta)

    @classmethod
    def failed(cls, code, message, retryable=False, reason=None):
        """A failed call. `message` reaches the model as the tool's output and
        the browser as the error's own sentence; `reason` is the shorter phrase
        a card replays on a reload, where the streamed frames are gone.
        """
        meta = {}
        if reason is not None:
            meta["reason"] = reason
        return cls(
            success=False,
            error={"code": code, "message": message, "retryable": retryable},
            metadata=meta,
            text=message,
        )


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable
    permission: str = READ_ONLY
    timeout: float = 30.0
    tags: tuple = ()
    # Whether this run may use the tool at all, beyond its permission. A tool
    # that only reads what the run was given has nothing to offer a run that
    # was given nothing, and its schema is prompt spent on every round.
    when: Callable | None = None
    # Whether the result carries a card the browser draws whole, under the
    # tool's own name. Declared beside the tool so a rename moves the card
    # with it, instead of leaving a list in another module pointing at a name
    # nothing registers any more.
    carded: bool = False
    # Whether the run's last pass may still call it. That pass has to end in an
    # answer, so only a tool that costs nothing to offer and writes no
    # answerable content is kept; everything else is withheld so the model
    # answers from what it has. Declared here for the same reason as `carded`:
    # the engine must not name a tool to know this.
    last_pass: bool = False

    def offered(self, ctx):
        return self.when is None or bool(self.when(ctx))

    def spec(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def add(self, tool):
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def remove(self, name):
        """Drop a tool. Used by tests that register a fixture tool into the
        shared registry and have to leave it as they found it."""
        return self._tools.pop(name, None)

    def get(self, name):
        return self._tools.get(name)

    def names(self):
        return set(self._tools)

    def all(self):
        return list(self._tools.values())

    def validate(self, tool, args):
        """Check a call's arguments against the tool's schema before anything
        runs. Returns (args, error). The tool is passed in rather than looked up
        again, so "is this registered" is answered in one place, by `call`."""
        try:
            jsonschema.validate(args, tool.parameters)
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path) or "(root)"
            return None, ToolError(INVALID_ARGUMENTS, f"{where}: {e.message}")
        except jsonschema.SchemaError as e:
            return None, ToolError(INVALID_ARGUMENTS, f"bad schema: {e.message}")
        return args, None

    async def call(self, name, args, ctx=None):
        """Validate, authorize, run under a timeout, and normalize the outcome.

        The argument dict is made here, once: a model's transport may hand the
        arguments over as a JSON string, and everything below this line works
        on a mapping. A handler's reply is normalized the same way, so a caller
        only ever sees a ToolResult.
        """
        ctx = ctx or ToolContext()
        args = as_args(args)
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failed(UNKNOWN_TOOL, f"no tool named {name!r}")
        args, err = self.validate(tool, args)
        if err:
            return ToolResult.failed(err.code, err.message, err.retryable)

        refusal = authorize(tool, ctx.workspace)
        if refusal:
            return ToolResult.failed(*refusal)

        try:
            result = await asyncio.wait_for(_invoke(tool, args, ctx), timeout=tool.timeout)
        except asyncio.TimeoutError:
            return ToolResult.failed(TIMEOUT, f"{name} exceeded {tool.timeout:g}s", retryable=True)
        except ToolError as e:
            return ToolResult.failed(e.code, e.message, e.retryable)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return ToolResult.failed(
                TOOL_ERROR, f"{type(e).__name__}: {e}", retryable=True
            )

        if isinstance(result, ToolResult):
            return result
        return ToolResult.ok(as_text(result))

    def specs_for(self, ctx, tags=None):
        """The tools this context may actually use, so the model is never
        offered a schema it would only be refused for.

        One pass over the registry answers both questions a run asks of it —
        which tags it wants and which of those this context can have — because
        the two answers are only ever used together.
        """
        wanted = set(tags) if tags else None
        return [t.spec() for t in self.all()
                if authorize(t, ctx.workspace) is None and t.offered(ctx)
                and (not wanted or wanted & set(t.tags))]


def as_args(args):
    """Tool arguments as a mapping. A transport may hand them over as a JSON
    string; anything that will not parse is an empty call rather than a crash."""
    if isinstance(args, dict):
        return args
    try:
        parsed = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _invoke(tool, args, ctx):
    """Handlers take (args, ctx) or just (args); both are supported.

    A sync handler is run in a thread. Called straight from here it would block
    the loop for every other request, and `wait_for` could not interrupt it.
    """
    fn = tool.handler
    take_ctx = len(inspect.signature(fn).parameters) >= 2
    if inspect.iscoroutinefunction(fn):
        return await fn(args, ctx) if take_ctx else await fn(args)
    # Not a coroutine function, but it may still return one, so the result is
    # checked rather than assumed.
    out = await asyncio.to_thread(fn, args, ctx) if take_ctx else await asyncio.to_thread(fn, args)
    return await out if inspect.isawaitable(out) else out


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def days_arg(args, default, low, high):
    """The `days` argument, clamped to what the tool can serve.

    The model writes the number as a string, leaves the argument off, or asks
    for a window wider than any source will give; all three arrive here and
    leave as a usable int. Every tool with a `days` parameter reads it here,
    so the clamping rule is stated once.
    """
    try:
        value = int(args.get("days") or default)
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


def missing_arg(what):
    """A call that named nothing. `what` is the noun the argument carries, so
    the sentence the model reads and the phrase a card replays stay the same
    shape across every tool that requires one."""
    return ToolResult.failed(TOOL_ERROR, f"No {what} given.", reason=f"no {what} was given")


def unreachable(service, exc, reason):
    """A service that could not be reached at all. `service` is the name the
    sentence leads with and `reason` the short phrase a card replays, so two
    tools reporting the same kind of failure say it the same way."""
    return ToolResult.failed(
        TOOL_ERROR,
        f"{service} could not be reached ({type(exc).__name__}).",
        reason=reason,
    )


def unreadable(service, reason):
    """A service that answered, with something that would not parse."""
    return ToolResult.failed(
        TOOL_ERROR,
        f"{service} sent a reply that could not be read.",
        reason=reason,
    )


def source_row(url, title, *, read=False):
    """One entry for a result's `sources`: where a figure came from, and
    whether its page text was actually read.

    `read` is the only thing that lets a card tell a page the answer may rest
    on from one that was never more than a snippet, so every tool states it
    rather than leaving the browser to assume.
    """
    return {"source": url, "title": title, "kind": "web", "read": read}


def source_view(row):
    """A source row as the browser reads it, whether it arrives on a live frame
    or out of a stored turn.

    Both sides carry the same fields, so a row that was streamed and the same
    row after a reload draw the same. `n` is not here: a live frame carries it
    beside the fields and a stored row inside them, and each stamps its own.
    """
    return {
        "source": row.get("source", ""),
        "title": row.get("title", ""),
        "kind": row.get("kind", "web"),
        "read": bool(row.get("read")),
    }


registry = Registry()
