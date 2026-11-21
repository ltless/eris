#Requires -Version 5.1
# Windows launcher for Eris. Twin of start.sh.
# Creates the venv, starts SearXNG (docker), the llama.cpp model server (if
# MODEL_PATH is set), then uvicorn. Ctrl-C stops what it started.

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        if ($t.StartsWith('export ')) { $t = $t.Substring(7).Trim() }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        # Strip one layer of matching quotes, same as a shell `source`.
        if ($v.Length -ge 2 -and
            (($v.StartsWith('"') -and $v.EndsWith('"')) -or
             ($v.StartsWith("'") -and $v.EndsWith("'")))) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        if ($k) { Set-Item -Path ("Env:" + $k) -Value $v }
    }
}

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}

Import-DotEnv (Join-Path $PSScriptRoot '.env')

function Resolve-Executable([string]$Name, [string[]]$Candidates) {
    # PATH first, then well-known install locations. A terminal opened before
    # an installer ran keeps a stale PATH, so probing the disk is not optional.
    if ($Name -and (Test-Path -LiteralPath $Name -PathType Leaf)) { return $Name }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    foreach ($p in $Candidates) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
    }
    return $null
}

function Add-ParentToPath([string]$ExePath) {
    $dir = Split-Path -Parent $ExePath
    if ($dir -and (($env:Path -split ';') -notcontains $dir)) {
        $env:Path = "$dir;$env:Path"
    }
}

function Get-InstallCandidates([string]$Relative) {
    $paths = @()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($base) { $paths += (Join-Path $base $Relative) }
    }
    return $paths
}

$Port = if ($env:PORT) { $env:PORT } else { '9000' }
$ErisHost = if ($env:ERIS_HOST) { $env:ERIS_HOST } else { '127.0.0.1' }
$ModelPath = if ($env:MODEL_PATH) { $env:MODEL_PATH } else { '' }
$LlamaBin = if ($env:LLAMA_BIN) { $env:LLAMA_BIN } else { 'llama-server' }
$LlamaPort = if ($env:LLAMA_PORT) { $env:LLAMA_PORT } else { '8080' }
$ModelBaseUrl = if ($env:MODEL_BASE_URL) { $env:MODEL_BASE_URL } else { 'http://localhost:11434/v1' }
$env:MODEL_BASE_URL = $ModelBaseUrl

# @(...) around the whole expression, not after each term: `+` on two strings
# concatenates them, so the parens here are what make this a list of paths.
$Docker = Resolve-Executable 'docker' @(
    @(Get-InstallCandidates 'Docker\Docker\resources\bin\docker.exe') +
    @(Get-InstallCandidates 'Programs\DockerDesktop\resources\bin\docker.exe')
)
if ($Docker) { Add-ParentToPath $Docker }

$LlamaExe = Resolve-Executable $LlamaBin @(
    @(Join-Path $PSScriptRoot 'llama-server.exe') +
    @(Join-Path $PSScriptRoot 'bin\llama-server.exe') +
    @(Get-InstallCandidates 'llama.cpp\llama-server.exe') +
    @(Get-InstallCandidates 'Microsoft\WinGet\Links\llama-server.exe')
)

$llamaProc = $null
$startedDocker = $false

try {
    # 1. venv + deps
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    # A venv copied from another machine still has python.exe, but it points at
    # a base interpreter this host does not have. Running it would fail with a
    # cryptic "No Python at ..." later, so check the recorded home instead.
    $venvCfg = Join-Path $PSScriptRoot '.venv\pyvenv.cfg'
    if (Test-Path -LiteralPath $venvPython) {
        $venvHome = Select-String -LiteralPath $venvCfg -Pattern '^home\s*=\s*(.+)$' |
            Select-Object -First 1
        if ($venvHome -and -not (Test-Path -LiteralPath $venvHome.Matches[0].Groups[1].Value.Trim())) {
            throw '.venv is broken (its base Python is missing on this machine). Delete .venv, then re-run.'
        }
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
        if (-not $python) { throw 'python not found on PATH (install Python 3.10+)' }
        Write-Host '> creating venv'
        & $python.Source -m venv .venv
    }
    & $venvPython -m pip install -q -r requirements.txt

    # 2. SearXNG
    if ($Docker) {
        # Docker Desktop may be installed but not started. That probe fails by
        # design, and native stderr is terminating under the top-level
        # ErrorActionPreference = 'Stop', so it has to run relaxed or the
        # script dies before it can print the message below.
        $prevPref = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $Docker info --format '{{.ServerVersion}}' *> $null
            $dockerUp = ($LASTEXITCODE -eq 0)
        } finally {
            $ErrorActionPreference = $prevPref
        }
        if ($dockerUp) {
            Write-Host '> starting docker services (searxng)'
            & $Docker compose up -d
            $startedDocker = $true
        } else {
            Write-Host '! Docker engine not running - start Docker Desktop, then re-run'
        }
    } else {
        Write-Host '! docker not found, skipping SearXNG'
    }

    # 3. llama.cpp model server
    if ($ModelPath -and (Test-Path -LiteralPath $ModelPath -PathType Leaf)) {
        if ($LlamaExe) {
            Write-Host "> starting $LlamaExe ($ModelPath)"
            $ModelBaseUrl = "http://127.0.0.1:${LlamaPort}/v1"
            $env:MODEL_BASE_URL = $ModelBaseUrl
            $llamaProc = Start-Process -FilePath $LlamaExe -PassThru -NoNewWindow -ArgumentList @(
                '--model', ('"{0}"' -f $ModelPath),
                '--host', '127.0.0.1',
                '--port', $LlamaPort,
                '--ctx-size', '32768',
                '--n-gpu-layers', '99',
                '--reasoning-format', 'deepseek'
            )
        } else {
            Write-Host "! $LlamaBin not found, skipping model server"
        }
    } else {
        Write-Host "! MODEL_PATH not set/found, assuming model server already running at $ModelBaseUrl"
    }

    # 4. wait for model
    if ($llamaProc) {
        for ($i = 0; $i -lt 60; $i++) {
            try {
                Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "$ModelBaseUrl/models" | Out-Null
                break
            } catch {
                Start-Sleep -Seconds 1
            }
        }
    }

    # 5. app
    Write-Host "> Eris on http://localhost:$Port"
    # Watching `app` rather than the whole tree: the default scan includes
    # .venv, where every installed package is a file to stat on each save.
    & $venvPython -m uvicorn app.api:app --host $ErisHost --port $Port --reload --reload-dir app
}
finally {
    if ($llamaProc -and -not $llamaProc.HasExited) {
        Stop-Process -Id $llamaProc.Id -Force -ErrorAction SilentlyContinue
    }
    if ($startedDocker) {
        # Shutdown must never throw over the try block's real error.
        $ErrorActionPreference = 'Continue'
        & $Docker compose stop *> $null
    }
}
