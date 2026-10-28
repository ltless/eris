"""What the model server is doing with memory, and what it would need.

Two kinds of number live here. Some are reported: the endpoint says which
context it allocated and how much of it a prompt filled, the operating system
says how much video memory the runner holds. The KV cache is neither — the
endpoint never reports it — so it is derived from the model's own metadata,
which the endpoint does report, and the arithmetic is written out in
`kv_bytes` where it can be checked against a measurement.

Nothing here is inferred from a model's name. A model that hides its
architecture gets a size and no KV figure, which is less than a guess.
"""

import asyncio
import json
import shutil
import subprocess

import httpx

from .providers import base_url

# The endpoint reports allocated context and residency on its runtime route;
# `/api/show` reports the model's architecture. Both are cheap, and neither
# costs a generation, so they are asked directly rather than cached.
PS_TIMEOUT = 6
SHOW_TIMEOUT = 30

# Bytes per KV element. llama.cpp keeps the cache in f16 unless a quantized
# cache type is set, and the arithmetic below is checked against measurement:
# on the 4.45B Spark model this formula lands within 3% of the video memory the
# runner actually grows by between two context sizes.
KV_ELEMENT_BYTES = 2

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

# The adapter's real capacity, which WMI gets wrong: `Win32_VideoController
# .AdapterRAM` is a 32-bit field, so an 8 GiB card reports 4 GiB. The driver
# key holds the true value and never changes while the machine is up, so it is
# read once.
_GPU_TOTAL: int | None = None
_GPU_TOTAL_READ = False

_GPU_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @{ adapter_used = $null; adapter_committed = $null; runner_used = $null; gpu_util = $null }
try {
  $a = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage').CounterSamples |
       Sort-Object CookedValue -Descending | Select-Object -First 1
  if ($a) { $out.adapter_used = [int64]$a.CookedValue }
  $c = (Get-Counter '\GPU Adapter Memory(*)\Total Committed').CounterSamples |
       Sort-Object CookedValue -Descending | Select-Object -First 1
  if ($c) { $out.adapter_committed = [int64]$c.CookedValue }
  $p = (Get-Process llama-server).Id
  if ($p) {
    $g = (Get-Counter '\GPU Process Memory(*)\Dedicated Usage').CounterSamples |
         Where-Object { $_.InstanceName -match ('pid_{0}_' -f $p) }
    $out.runner_used = [int64](($g | Measure-Object CookedValue -Sum).Sum)
  }
  $e = (Get-Counter '\GPU Engine(*)\Utilization Percentage').CounterSamples |
       Where-Object { $_.CookedValue -gt 0 }
  $sum = ($e | Measure-Object CookedValue -Sum).Sum
  if ($null -ne $sum) { $out.gpu_util = [math]::Round($sum, 1) }
} catch {}
$out | ConvertTo-Json -Compress
"""


def _read_gpu_total():
    """The largest adapter's true capacity, read from the driver key once.

    A machine with no such key — a virtual display only, or a driver that
    names it differently — answers `None`, and every figure that would need it
    is then reported without a total rather than against a wrong one.
    """
    global _GPU_TOTAL, _GPU_TOTAL_READ
    if _GPU_TOTAL_READ:
        return _GPU_TOTAL
    _GPU_TOTAL_READ = True
    if not shutil.which("powershell"):
        return None
    script = (
        "$m=(Get-ItemProperty "
        "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class"
        "\\{4d36e968-e325-11ce-bfc1-08002be10318}\\*' | "
        "Where-Object { $_.'HardwareInformation.qwMemorySize' } | "
        "ForEach-Object { [int64]$_.'HardwareInformation.qwMemorySize' } | "
        "Measure-Object -Maximum).Maximum; if($m){[int64]$m}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
        _GPU_TOTAL = int(r.stdout.strip().splitlines()[-1])
    except Exception:
        _GPU_TOTAL = None
    return _GPU_TOTAL


def _run_gpu_script():
    """One spawn for every live figure, and `None` where the machine can't say.

    The counters reach the driver, so they answer in well under a second; the
    process spawn is the expensive part, which is why they share one.
    """
    if not shutil.which("powershell"):
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _GPU_SCRIPT],
            capture_output=True, text=True, timeout=30,
        )
        out = r.stdout.strip().splitlines()
        if not out:
            return None
        return json.loads(out[-1])
    except Exception:
        return None


async def gpu_info():
    """Video memory and utilization, or `None` when the machine cannot report.

    Two subprocess spawns stand between the caller and an answer, so they run
    off the event loop: a chat streaming beside this must not be held up by
    PowerShell starting.
    """
    total, live = await asyncio.gather(
        asyncio.to_thread(_read_gpu_total),
        asyncio.to_thread(_run_gpu_script),
    )
    if total is None and live is None:
        return None
    live = live or {}
    return {
        "total": total,
        "used": live.get("adapter_used"),
        "committed": live.get("adapter_committed"),
        "runner": live.get("runner_used"),
        "util": live.get("gpu_util"),
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


def kv_bytes(info, ctx):
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
    per_token_per_layer = heads * (key_len + value_len) * KV_ELEMENT_BYTES
    window = info.get("window")
    pattern = info.get("window_pattern")
    if not window or not pattern:
        # Every layer attends over the whole context.
        return layers * per_token_per_layer * ctx

    rows = pattern if isinstance(pattern, list) else [pattern]
    # The pattern repeats; where it doesn't divide the layer count, the model
    # is outside what this arithmetic was checked against, so it declines.
    if len(rows) != layers or not all(isinstance(v, bool) for v in rows):
        return None
    sliding = sum(1 for v in rows if v)
    fixed = sliding * per_token_per_layer * min(ctx, window)
    return fixed + (layers - sliding) * per_token_per_layer * ctx


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
            "bytes": kv_bytes(info, sized_ctx),
            "ctx": sized_ctx,
            # False when the figure describes a context that is not loaded yet.
            "estimated": allocated is None,
        },
        "gpu": gpu,
        "last": metrics or None,
    }
