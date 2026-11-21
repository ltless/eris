"""Read model and runner metadata without a generation request."""

import re
import time

import httpx

from .client import client
from .endpoint import auth_headers, base_url, model_for

_TTL = 300
_SHOW = {}
_PROPS = {}
_WINDOWS = {}


def positive_int(value):
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value) or None
    return None


def _key(model):
    return base_url(), model_for(model)


def _cached(cache, model):
    found = cache.get(_key(model))
    if found is not None and time.monotonic() - found[0] < _TTL:
        return found[1]
    return None


async def _read(method, path, timeout, **kwargs):
    try:
        response = await getattr(client(), method)(
            base_url() + path, headers=auth_headers(), timeout=timeout, **kwargs
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cached_model_info(model):
    return _cached(_SHOW, model)


async def model_info(model):
    cached = cached_model_info(model)
    if cached is not None:
        return cached
    shown = await _read("post", "/api/show", 5, json={"model": model_for(model)})
    if shown:
        _SHOW[_key(model)] = (time.monotonic(), shown)
    return shown


def cached_properties(model):
    return _cached(_PROPS, model)


async def server_properties(model):
    cached = cached_properties(model)
    if cached is not None:
        return cached
    props = await _read("get", "/props", 3, params={"model": model_for(model), "autoload": "false"})
    if props and llama_window(props):
        _PROPS[_key(model)] = (time.monotonic(), props)
        return props
    return None


async def runner_report(model, *, timeout=4):
    reported = await _read("get", "/api/ps", timeout)
    rows = (reported or {}).get("models")
    if not isinstance(rows, list):
        return False, None
    for row in rows:
        if isinstance(row, dict) and model in (row.get("name"), row.get("model")):
            _WINDOWS[_key(model)] = (time.monotonic(), positive_int(row.get("context_length")))
            return True, row
    _WINDOWS.pop(_key(model), None)
    return True, None


async def runtime_status(model, *, timeout=4):
    _, row = await runner_report(model, timeout=timeout)
    return row


def cached_window(model):
    return _cached(_WINDOWS, model)


def llama_window(props):
    defaults = (props or {}).get("default_generation_settings")
    return positive_int(defaults.get("n_ctx")) if isinstance(defaults, dict) else None


def model_context(shown):
    info = (shown or {}).get("model_info") or {}
    if not isinstance(info, dict):
        return None
    arch = info.get("general.architecture")
    if arch:
        return positive_int(info.get(f"{arch}.context_length"))
    limits = {positive_int(value) for key, value in info.items() if key.endswith(".context_length")}
    limits.discard(None)
    return limits.pop() if len(limits) == 1 else None


def configured_context(shown):
    parameters = (shown or {}).get("parameters")
    if isinstance(parameters, dict):
        return positive_int(parameters.get("num_ctx"))
    if isinstance(parameters, str):
        match = re.search(r"^num_ctx\s+(\d+)\s*$", parameters, re.MULTILINE)
        return positive_int(match[1]) if match else None
    return None


def requested_context(value, maximum=None):
    value = positive_int(value)
    return min(value, maximum) if value and maximum else value
