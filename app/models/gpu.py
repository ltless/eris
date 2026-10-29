"""Video memory, as the machine reports it.

Everything here reaches the operating system rather than the model server: two
PowerShell spawns, one for the counters that move and one for the capacity that
does not. Both are wrapped so a machine that cannot answer reports `None` for
each figure instead of failing the panel that asked.
"""

import asyncio
import json
import shutil
import subprocess

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
