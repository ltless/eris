"""Tools the model can call. Each module exposes a tool definition + handler."""

import json


def as_args(args):
    """Tool arguments arrive as a dict, or as a JSON string from some models."""
    if isinstance(args, dict):
        return args
    try:
        parsed = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
