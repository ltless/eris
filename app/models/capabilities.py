"""What a backend can actually do.

Capabilities are read from configuration and from what the endpoint reports,
never inferred from a model's name: two models served by one llama-server can
differ in whether they call tools, and guessing wrong produces a tool call the
server ignores or a request it rejects.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capabilities:
    chat: bool = True
    streaming: bool = True
    tools: bool = True
    structured: bool = False
    vision: bool = False
    reasoning: bool = False
    context: int = 0  # tokens; 0 means "whatever the server was started with"

    def supports(self, name):
        return bool(getattr(self, name, False))


# The local llama.cpp / Ollama servers this app is built against accept tool
# schemas, stream NDJSON, and take `think`. These are the defaults; `.env` can
# turn any of them off.
LOCAL = Capabilities(tools=True, reasoning=True, vision=True)

# An OpenAI-compatible endpoint in front of a hosted model: same request
# shape, no guarantee about reasoning.
OPENAI_COMPATIBLE = Capabilities(tools=True, structured=True, vision=True)

PROFILES = {
    "local": LOCAL,
    "openai": OPENAI_COMPATIBLE,
}


def for_configured_backend(profile="local"):
    """Capabilities for the single configured backend.

    One endpoint serves every role here, so this is a lookup rather than a
    parallel set of per-model tables. When a second backend is added it becomes
    a per-provider field, not a rename of this function.
    """
    return PROFILES.get(profile, LOCAL)


@dataclass
class ModelInfo:
    """One selectable model and what it can do."""

    name: str
    capabilities: Capabilities = field(default_factory=Capabilities)
    healthy: bool | None = None
    note: str = ""

    def as_dict(self):
        caps = [
            k
            for k in ("chat", "streaming", "tools", "structured", "vision",
                      "reasoning")
            if getattr(self.capabilities, k)
        ]
        return {
            "name": self.name,
            "capabilities": caps,
            "context": self.capabilities.context,
            "healthy": self.healthy,
            "note": self.note,
        }
