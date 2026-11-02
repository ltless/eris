"""What the model server is doing with memory, and what it would need.

The endpoint never reports KV cache use, so it is derived from the model
metadata in `kv_bytes` rather than guessed. Nothing here is inferred from a
name: a model that hides its architecture gets no KV figure. Video memory comes
from `gpu` instead.
"""

import asyncio

import httpx

from .endpoint import base_url
from .gpu import gpu_info
from ..config import settings

# The endpoint reports allocated context and residency on its runtime route;
# `/api/show` reports the model's architecture. Both are cheap, and neither
# costs a generation, so they are asked directly rather than cached.
PS_TIMEOUT = 6
SHOW_TIMEOUT = 30

# Bytes per KV element, by the cache type the server was started with. llama.cpp
# stores each block of 32 elements as the elements plus one scale (f32 for
# q8_0, f16 for q4_0), so the quantized ratios are not exactly 1/2 and 1/4:
# q8_0 is 34 bytes per 32, q4_0 is 18. The default is the unquantized f16.
#
# Ollama accepts this only as a server-wide `OLLAMA_KV_CACHE_TYPE` and does not
# report it on any route, so it cannot be detected; `KV_CACHE_TYPE` mirrors it.
KV_BYTES = {
    "f16": 2.0,
    "q8_0": 34 / 32,
    "q4_0": 18 / 32,
}

# The architecture's own metadata is keyed by its prefix, so a key ending in
# one of these names the field regardless of which model it describes.
_KV_FIELDS = {
    "layers": "block_count",
    "kv_heads": "attention.head_count_kv",
    "key_length": "attention.key_length",
    "value_length": "attention.value_length",
    "window": "attention.sliding_window",
    "window_pattern": "attention.sliding_window_pattern",
    "context": "context_length",
}



def architecture(model_info):
    """The model's architecture fields, by their plain names.

    The metadata is keyed with the architecture as a prefix — `spark2_5
    .block_count` — and the prefix differs per model, so it is found rather
    than assumed. A field the model doesn't state is simply absent, and
    `kv_bytes` refuses to guess past that.
    """
    arch = None
    for key in model_info or ():
        if key.endswith(".block_count"):
            arch = key[: -len(".block_count")]
            break
    if not arch:
        return {}
    out = {}
    for name, suffix in _KV_FIELDS.items():
        value = model_info.get(f"{arch}.{suffix}")
        if value is not None:
            out[name] = value
    return out


def element_bytes(cache_type=None):
    """Bytes per KV element for a cache type name, or `None` if it is unknown.

    An unrecognised name is not quietly read as f16: a typo in `KV_CACHE_TYPE`
    would then turn into a figure that looks measured and is wrong, which is
    worse than the panel having no figure at all.
    """
    name = (cache_type or settings.kv_cache_type or "f16").strip().lower()
    return KV_BYTES.get(name)


def kv_bytes(info, ctx, cache_type=None):
    """The KV cache this context costs, in bytes, and `None` if unknowable.

    The cache is two tensors per layer per head, sized by the head's own key
    and value length, and grown to the context. A model that limits most of its
    layers to a sliding window stops paying for those layers past the window,
    so they contribute a fixed amount and only the global layers keep growing
    with the context — which is the whole difference between a model that
    reads a long prompt cheaply and one that does not.
    """
    layers = info.get("layers")
    heads = info.get("kv_heads")
    key_len = info.get("key_length")
    value_len = info.get("value_length")
    if not all(isinstance(v, int) and v > 0 for v in (layers, heads, key_len, value_len)):
        return None
    if not ctx:
        return None
    element = element_bytes(cache_type)
    if element is None:
        return None
    per_token_per_layer = heads * (key_len + value_len) * element
    window = info.get("window")
    pattern = info.get("window_pattern")
    if not window or not pattern:
        # Every layer attends over the whole context.
        return int(round(layers * per_token_per_layer * ctx))

    rows = pattern if isinstance(pattern, list) else [pattern]
    # One entry per layer. A shorter pattern is not expanded to fill the layer
    # count, because that arithmetic was never checked against a model that
    # states its window that way, so those models get no figure.
    if len(rows) != layers or not all(isinstance(v, bool) for v in rows):
        return None
    sliding = sum(1 for v in rows if v)
    fixed = sliding * per_token_per_layer * min(ctx, window)
    return int(round(fixed + (layers - sliding) * per_token_per_layer * ctx))


def _num(value):
    """A count the endpoint reports as a number, or `None`.

    An integer field parsed out of JSON can arrive as an int, a float, or a
    string depending on the server, and a dashboard that prints `None` for a
    token count is worse than one that omits the row.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def runtime_status(model):
    """Which model the runner holds, on which context, and until when.

    An empty answer is a real answer: a model that is not loaded is not in
    memory, and saying so is the point of asking. The route is the native one
    because the loaded set is a runtime fact, not a model-list fact.
    """
    try:
        async with httpx.AsyncClient(timeout=PS_TIMEOUT) as c:
            r = await c.get(base_url() + "/api/ps")
        if r.status_code >= 400:
            return None
        rows = r.json().get("models") or []
    except Exception:
        return None
    for row in rows:
        if row.get("name") == model or row.get("model") == model:
            return row
    return None


async def model_info(model):
    """The model's own metadata, as the endpoint reports it."""
    try:
        async with httpx.AsyncClient(timeout=SHOW_TIMEOUT) as c:
            r = await c.post(base_url() + "/api/show", json={"model": model})
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None


async def model_stats(model, ctx=None, metrics=None):
    """Everything the panel shows, gathered in one place.

    `ctx` is the context this account would ask for; the allocated context wins
    when the model is already loaded, because that is the one the runner
    actually reserved. `metrics` are the counts from the last streamed answer,
    which are the only place prompt usage is ever stated.
    """
    loaded, shown, gpu = await asyncio.gather(
        runtime_status(model),
        model_info(model),
        gpu_info(),
    )
    info = architecture((shown or {}).get("model_info") or {})
    allocated = _num((loaded or {}).get("context_length"))
    # The runner's own reservation is what KV is really sized against; before
    # it loads, the figure is an estimate for the context that would be asked
    # for, which is why the answer says which one it is.
    sized_ctx = allocated or ctx or _num(info.get("context"))
    cache_type = (settings.kv_cache_type or "f16").strip().lower()
    return {
        "model": model,
        "loaded": loaded is not None,
        "expires_at": (loaded or {}).get("expires_at"),
        "size": _num((loaded or {}).get("size")),
        "size_vram": _num((loaded or {}).get("size_vram")),
        "runner": (loaded or {}).get("runner"),
        "details": (loaded or {}).get("details") or (shown or {}).get("details") or {},
        "context": {
            "max": _num(info.get("context")),
            "allocated": allocated,
            "wanted": _num(ctx),
        },
        "kv": {
            "bytes": kv_bytes(info, sized_ctx, cache_type),
            "ctx": sized_ctx,
            # False when the figure describes a context that is not loaded yet.
            "estimated": allocated is None,
            # The cache type this figure was computed for. The server states it
            # nowhere, so it is named here rather than left to be assumed: a
            # reader can then tell a figure that is out of date from one that
            # was never claimed.
            "type": cache_type,
            "known": element_bytes(cache_type) is not None,
        },
        "gpu": gpu,
        "last": metrics or None,
    }
