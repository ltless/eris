"""Which model answers which kind of work.

Deliberately small and explainable: the user's chosen model serves every role,
and the roles exist so that a later change can point one of them elsewhere
without touching the runtime. Nothing here switches backends on failure — a
local model that is down stays down until the operator says otherwise.
"""

from dataclasses import dataclass

# Roles the runtime asks for. They are capability requirements, not model names.
CHAT = "chat"
RESEARCH = "research"
VERIFICATION = "verification"

ROLES = (CHAT, RESEARCH, VERIFICATION)


@dataclass(frozen=True)
class Choice:
    model: str | None
    role: str
    reason: str
    requires: tuple = ()


class NoModelAvailable(Exception):
    """Nothing configured can serve the role. The caller turns this into a
    message naming the role and what it needs."""


class Router:
    """Resolves a role to a model, from per-user preferences and config."""

    def __init__(self, settings, overrides=None):
        self.settings = settings
        self.overrides = dict(overrides or {})

    def choose(self, role=CHAT, prefs=None, requested=None):
        prefs = prefs or {}
        # An explicit choice in the request wins; then the account's saved
        # model; then the configured default. A role override is the operator
        # pinning a role in config and takes precedence over the saved model
        # only for that role.
        model = requested or prefs.get("model") or self.overrides.get(role) or self.settings.model_name
        if not model:
            raise NoModelAvailable(
                f"no model is configured for {role}: set MODEL_NAME in .env or pick one in Settings"
            )
        requires = (role,) if role != CHAT else ()
        why = "requested" if requested else ("account default" if prefs.get("model") else "server default")
        return Choice(model=model, role=role, reason=why, requires=requires)


def build(settings):
    """The process-wide router. One configured backend, so nothing to vary."""
    return Router(settings)
