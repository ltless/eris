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

from . import policy
from ..runtime.errors import INVALID_ARGUMENTS, UNKNOWN_TOOL, ToolError

@dataclass
class ToolContext:
    user_id: int | None = None
    approved: set = field(default_factory=set)


@dataclass(frozen=True)
class ToolResult:
    """What a handler returns. `data` is the structured payload; `text` is what
    the model reads. A tool that answers the model in prose fills `text`."""

    success: bool = True
    data: dict | None = None
    error: dict | None = None
    metadata: dict = field(default_factory=dict)
    text: str | None = None

    @classmethod
    def ok(cls, text=None, **data):
        return cls(success=True, data=data or None, text=text)

    @classmethod
    def failed(cls, code, message, retryable=False, metadata=None):
        """A failed call. `metadata` is a dict rather than **kwargs, because a
        tool's own result often carries a key named `code` — a test run reports
        its exit code — and that would collide with this signature."""
        return cls(
            success=False,
            error={"code": code, "message": message, "retryable": retryable},
            metadata=dict(metadata or {}),
        )


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable
    permission: str = policy.READ_ONLY
    timeout: float = 30.0
    tags: tuple = ()
    side_effect: str = "none"

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
    """Holder for the registered tools, keyed by name."""

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

    def specs(self, tags=None):
        """Tool definitions for the model. No tag means every tool."""
        tools = self.all()
        if tags:
            wanted = set(tags)
            tools = [t for t in tools if wanted & set(t.tags)]
        return [t.spec() for t in tools]

    def validate(self, name, args):
        """Check a call before anything runs. Returns (args, error)."""
        tool = self._tools.get(name)
        if tool is None:
            return None, ToolError(UNKNOWN_TOOL, f"no tool named {name!r}")
        if not isinstance(args, dict):
            return None, ToolError(INVALID_ARGUMENTS, "arguments must be an object")
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

        A handler may return a ToolResult, a plain string for the model, or a
        list of records; the last two are wrapped so callers only ever see one
        shape.
        """
        ctx = ctx or ToolContext()
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failed(UNKNOWN_TOOL, f"no tool named {name!r}")
        args, err = self.validate(name, args)
        if err:
            return ToolResult.failed(err.code, err.message, err.retryable)

        decision = policy.authorize(tool, ctx, args)
        if not decision.allowed:
            return ToolResult.failed(decision.code, decision.reason)
        args = decision.rewrite(args)

        try:
            result = await asyncio.wait_for(_invoke(tool, args, ctx), timeout=tool.timeout)
        except asyncio.TimeoutError:
            return ToolResult.failed("timeout", f"{name} exceeded {tool.timeout:g}s", retryable=True)
        except ToolError as e:
            return ToolResult.failed(e.code, e.message, e.retryable)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return ToolResult.failed(
                "tool_error", f"{type(e).__name__}: {e}", retryable=True
            )

        if isinstance(result, ToolResult):
            return result
        if isinstance(result, str):
            return ToolResult.ok(result)
        return ToolResult.ok(_as_text(result), items=result)

    def specs_for(self, ctx):
        """The tools this context may actually use.

        Approval-gated tools are offered: the model can ask, and the ask is
        what the user is shown.
        """
        out = []
        for t in self.all():
            d = policy.authorize(t, ctx)
            if d.allowed or d.code == "approval_required":
                out.append(t.spec())
        return out


async def _invoke(tool, args, ctx):
    """Handlers take (args, ctx) or just (args); both are supported, and a
    sync handler runs off the event loop."""
    fn = tool.handler
    take_ctx = len(inspect.signature(fn).parameters) >= 2
    out = fn(args, ctx) if take_ctx else fn(args)
    if inspect.isawaitable(out):
        return await out
    return out


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


registry = Registry()


def register_builtin(module, *, name=None, description=None, permission=policy.READ_ONLY,
                     timeout=30.0, tags=("chat",), handler=None, side_effect="none"):
    """Wrap an existing tools/<module>.py, which exposes TOOL and run().

    `run` is called as the module already calls it, so the module keeps working
    on its own for its own tests.
    """
    spec = module.TOOL["function"]
    return registry.add(
        Tool(
            name=name or spec["name"],
            description=description or spec["description"],
            parameters=spec["parameters"],
            handler=handler or module.run,
            permission=permission,
            timeout=timeout,
            tags=tuple(tags),
            side_effect=side_effect,
        )
    )
