#Requires -Version 5.1
# Start a local llama.cpp OpenAI-compatible server. Twin of run-model.sh.
# Requires llama.cpp built with a FILKAN backend (or any backend).

param(
    [string]$Model = './models/model.gguf'
)

$Port = if ($env:PORT) { $env:PORT } else { '8080' }
$LlamaBin = if ($env:LLAMA_BIN) { $env:LLAMA_BIN } else { 'llama-server' }

$candidates = @(
    (Join-Path $PSScriptRoot 'llama-server.exe') +
    (Join-Path $PSScriptRoot 'bin\llama-server.exe')
)
foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
    if ($base) {
        $candidates += (Join-Path $base 'llama.cpp\llama-server.exe')
        $candidates += (Join-Path $base 'Microsoft\WinGet\Links\llama-server.exe')
    }
}

$LlamaExe = $null
if (Test-Path -LiteralPath $LlamaBin -PathType Leaf) {
    $LlamaExe = $LlamaBin
} else {
    $cmd = Get-Command $LlamaBin -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $LlamaExe = $cmd.Source }
}
if (-not $LlamaExe) {
    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { $LlamaExe = $p; break }
    }
}
if (-not $LlamaExe) {
    throw "$LlamaBin not found on PATH or in the usual install locations. Set LLAMA_BIN to llama-server.exe's full path."
}

& $LlamaExe `
    --model $Model `
    --port $Port `
    --host 0.0.0.0 `
    --ctx-size 8192 `
    --n-gpu-layers 99 `
    --reasoning-format deepseek