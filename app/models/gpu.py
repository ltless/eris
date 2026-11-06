"""Video memory, as the machine reports it.

Everything here reaches the operating system rather than the model server: one
PowerShell spawn reads the counters that move and the capacity that does not.
It is wrapped so a machine that cannot answer reports `None` for each figure
instead of failing the panel that asked.
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

# A CIM query, not `Get-Counter`: the same perf counters are exposed as CIM
# classes, and one class read costs tens of milliseconds where a counter set
# costs over a second. The registry read for the adapter's capacity is in the
# same spawn, so a poll is one process instead of three.
_GPU_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @{ adapter_used = $null; adapter_committed = $null; runner_used = $null;
          gpu_util = $null; total = $null }
try {
  $a = Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory |
       Sort-Object DedicatedUsage -Descending | Select-Object -First 1
  if ($a) {
    $out.adapter_used = [int64]$a.DedicatedUsage
    $out.adapter_committed = [int64]$a.TotalCommitted
  }
  $p = (Get-Process llama-server).Id
  if ($p) {
    $g = Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory |
         Where-Object { $_.Name -match ('^pid_{0}_' -f $p) }
    $sum = ($g | Measure-Object DedicatedUsage -Sum).Sum
    if ($null -ne $sum) { $out.runner_used = [int64]$sum }
  }
  $e = Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine |
       Where-Object { $_.UtilizationPercentage -gt 0 }
  $sum = ($e | Measure-Object UtilizationPercentage -Sum).Sum
  if ($null -ne $sum) { $out.gpu_util = [math]::Round($sum, 1) }
  $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\*'
  $m = (Get-ItemProperty $key |
        Where-Object { $_.'HardwareInformation.qwMemorySize' } |
        ForEach-Object { [int64]$_.'HardwareInformation.qwMemorySize' } |
        Measure-Object -Maximum).Maximum
  if ($m) { $out.total = [int64]$m }
} catch {}
$out | ConvertTo-Json -Compress
"""


def _run_gpu_script():
    """One spawn for every figure, and `None` where the machine can't say."""
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


def _capacity(live):
    """The adapter's capacity, read once and remembered.

    A machine with no such key — a virtual display only, or a driver that names
    it differently — answers `None`, and every figure that would need it is then
    reported without a total rather than against a wrong one.
    """
    global _GPU_TOTAL, _GPU_TOTAL_READ
    if not _GPU_TOTAL_READ:
        _GPU_TOTAL_READ = True
        total = (live or {}).get("total")
        _GPU_TOTAL = int(total) if isinstance(total, (int, float)) and total > 0 else None
    return _GPU_TOTAL


async def gpu_info():
    """Video memory and utilization, or `None` when the machine cannot report.

    A subprocess spawn stands between the caller and an answer, so it runs off
    the event loop: a chat streaming beside this must not be held up by
    PowerShell starting.
    """
    live = await asyncio.to_thread(_run_gpu_script)
    total = _capacity(live)
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