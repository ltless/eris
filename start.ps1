#Requires -Version 5.1
# Windows launcher for Eris. Twin of start.sh.
# Creates the venv, starts ChromaDB + SearXNG (docker), the llama.cpp model
# server (if MODEL_PATH is set), then uvicorn. Ctrl-C stops what it started.

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
$ModelPath = if ($env:MODEL_PATH) { $env:MODEL_PATH } else { '' }
$LlamaBin = if ($env:LLAMA_BIN) { $env:LLAMA_BIN } else { 'llama-server' }
$ModelBaseUrl = if ($env:MODEL_BASE_URL) { $env:MODEL_BASE_URL } else { 'http://localhost:8080/v1' }

$Docker = Resolve-Executable 'docker' @(
    (Get-InstallCandidates 'Docker\Docker\resources\bin\docker.exe') +
    (Get-InstallCandidates 'Programs\DockerDesktop\resources\bin\docker.exe')
)
if ($Docker) { Add-ParentToPath $Docker }

$LlamaExe = Resolve-Executable $LlamaBin @(
    (Join-Path $PSScriptRoot 'llama-server.exe') +
    (Join-Path $PSScriptRoot 'bin\llama-server.exe') +
    (Get-InstallCandidates 'llama.cpp\llama-server.exe') +
    (Get-InstallCandidates 'Microsoft\WinGet\Links\llama-server.exe')
)

$llamaProc = $null
$startedDocker = $false

try {
    # 1. venv + deps
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
        if (-not $python) { throw 'python not found on PATH (install Python 3.10+)' }
        Write-Host '> creating venv'
        & $python.Source -m venv .venv
    }
    & $venvPython -m pip install -q -r requirements.txt

    # 2. ChromaDB + SearXNG
    if ($Docker) {
        # Docker Desktop may be installed but not started.
        & $Docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host '> starting docker services (chromadb, searxng)'
            & $Docker compose up -d
            $startedDocker = $true
        } else {
            Write-Host '! Docker engine not running - start Docker Desktop, then re-run'
        }
    } else {
        Write-Host '! docker not found, skipping ChromaDB'
    }

    # 3. llama.cpp model server
    if ($ModelPath -and (Test-Path -LiteralPath $ModelPath -PathType Leaf)) {
        if ($LlamaExe) {
            Write-Host "> starting $LlamaExe ($ModelPath)"
            $llamaProc = Start-Process -FilePath $LlamaExe -PassThru -NoNewWindow -ArgumentList @(
                '--model', $ModelPath,
                '--host', '0.0.0.0',
                '--port', '8080',
                '--ctx-size', '8192',
                '--embeddings',
                '--pooling', 'mean',
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
    & $venvPython -m uvicorn app.api:app --host 0.0.0.0 --port $Port
}
finally {
    if ($llamaProc -and -not $llamaProc.HasExited) {
        Stop-Process -Id $llamaProc.Id -Force -ErrorAction SilentlyContinue
    }
    if ($startedDocker) {
        & $Docker compose stop 2>$null | Out-Null
    }
}