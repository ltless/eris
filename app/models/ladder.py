"""Reasoning controls declared by the selected model server."""

from dataclasses import dataclass

from .metadata import cached_model_info, cached_properties, model_info, server_properties


@dataclass(frozen=True)
class Reasoning:
    values: tuple[bool | str, ...] = ()
    default: bool | str | None = None
    source: str = "unavailable"


def _same(a, b):
    return type(a) is type(b) and a == b


def supports(profile, value):
    return any(_same(value, option) for option in profile.values)


def _values(items):
    values = []
    if isinstance(items, list):
        for value in items:
            if (type(value) is bool or isinstance(value, str) and value.strip()) and not any(
                _same(value, old) for old in values
            ):
                values.append(value)
    return tuple(values)


def _ollama(shown):
    metadata = shown.get("thinking")
    if isinstance(metadata, dict):
        settings = Reasoning(_values(metadata.get("values")), source="metadata")
        default = metadata.get("default")
        return Reasoning(settings.values, default if supports(settings, default) else None,
                         "metadata")
    capabilities = shown.get("capabilities")
    if not isinstance(capabilities, list):
        return Reasoning()
    if "thinking" not in capabilities:
        return Reasoning(source="unsupported")
    model_info = shown.get("model_info")
    architecture = model_info.get("general.architecture") if isinstance(model_info, dict) else None
    if architecture in {"gpt-oss", "gptoss"}:
        return Reasoning(("low", "medium", "high"), "medium", "legacy")
    return Reasoning((False, True), source="legacy")


def _llama(props):
    caps = props.get("chat_template_caps") or {}
    if not isinstance(caps, dict):
        return Reasoning()
    rejected = caps.get("reasoning_effort_rejected") or ()
    levels = tuple(level for level in _values(caps.get("reasoning_effort_levels"))
                   if isinstance(level, str) and level not in rejected)
    settings = props.get("default_generation_settings")
    defaults = settings.get("params") if isinstance(settings, dict) else None
    default = defaults.get("reasoning_effort") if isinstance(defaults, dict) else None
    return Reasoning(levels, default if default in levels else None,
                     "metadata" if levels else "unavailable")


def cached_reasoning(model):
    shown = cached_model_info(model)
    if shown is not None:
        return _ollama(shown)
    props = cached_properties(model)
    return _llama(props) if props is not None else Reasoning()


async def reasoning(model):
    shown = await model_info(model)
    if shown is not None:
        return _ollama(shown)
    props = await server_properties(model)
    return _llama(props) if props is not None else Reasoning()


def saved_effort(prefs, model, legacy_model, profile):
    values = prefs.get("efforts")
    if isinstance(values, dict) and model in values:
        value = values[model]
        return value if supports(profile, value) else None
    index = prefs.get("effort")
    if model == legacy_model and type(index) is int and 0 <= index < len(profile.values):
        return profile.values[index]
    return None


def effort_label(value):
    if value is None:
        return "Default"
    if value is False or value == "none":
        return "Off"
    if value is True:
        return "On"
    return value.replace("_", " ").capitalize()


def request_effort(profile, value):
    if value is None or profile.source == "unavailable":
        return value
    if supports(profile, value):
        return value
    if value is False and profile.values:
        return profile.values[0]
    return None
