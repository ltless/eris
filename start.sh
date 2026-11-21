#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env
set -a; source .env; set +a

PORT="${PORT:-9000}"
ERIS_HOST="${ERIS_HOST:-127.0.0.1}"
MODEL_PATH="${MODEL_PATH:-}"
LLAMA_BIN="${LLAMA_BIN:-llama-server}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
export MODEL_BASE_URL="${MODEL_BASE_URL:-http://localhost:11434/v1}"

started_docker=0
cleanup() {
  [ -n "${llama_pid:-}" ] && kill "$llama_pid" 2>/dev/null || true
  [ "$started_docker" = 1 ] && docker compose stop >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# 1. venv + deps
if [ ! -d .venv ]; then
  echo "> creating venv"
  for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null; then
      "$candidate" -m venv .venv && break
    fi
  done
  [ -d .venv ] || { echo "! python not found on PATH (install Python 3.10+)" >&2; exit 1; }
fi
# POSIX venvs put the interpreter in bin/, Windows venvs in Scripts/.
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then
  PY=.venv/Scripts/python.exe
else
  PY=
fi
# A venv built on another machine keeps a python.exe that points at a base
# interpreter this host does not have. It exists but cannot run anything.
if [ -n "$PY" ] && ! "$PY" -c '' >/dev/null 2>&1; then
  echo "! .venv is broken (its base Python is missing on this machine)" >&2
  echo "  delete .venv, then re-run this script" >&2
  exit 1
fi
if [ -z "$PY" ]; then
  echo "! .venv has no usable interpreter, delete it and re-run" >&2
  exit 1
fi
"$PY" -m pip install -q -r requirements.txt

# 2. SearXNG
if command -v docker >/dev/null; then
  # The CLI is on PATH whether or not the engine is up, and `compose up` dies
  # under set -e when it is not. Probe first so the engine being down is a
  # warning, not a crash.
  if docker info >/dev/null 2>&1; then
    echo "> starting docker services (searxng)"
    docker compose up -d
    started_docker=1
  else
    echo "! Docker engine not running - start Docker Desktop, then re-run"
  fi
else
  echo "! docker not found, skipping SearXNG"
fi

# 3. llama.cpp model server
if [ -n "$MODEL_PATH" ] && [ -f "$MODEL_PATH" ]; then
  if command -v "$LLAMA_BIN" >/dev/null; then
    echo "> starting $LLAMA_BIN ($MODEL_PATH)"
    export MODEL_BASE_URL="http://127.0.0.1:$LLAMA_PORT/v1"
    "$LLAMA_BIN" --model "$MODEL_PATH" --host 127.0.0.1 --port "$LLAMA_PORT" \
      --ctx-size 32768 \
      --n-gpu-layers 99 \
      --reasoning-format deepseek &
    llama_pid=$!
  else
    echo "! $LLAMA_BIN not found, skipping model server"
  fi
else
  echo "! MODEL_PATH not set/found, assuming model server already running at $MODEL_BASE_URL"
fi

# 4. wait for model
if [ -n "${llama_pid:-}" ]; then
  for _ in $(seq 1 60); do
    curl -sf "$MODEL_BASE_URL/models" >/dev/null && break
    sleep 1
  done
fi

# 5. app
echo "> Eris on http://localhost:$PORT"
# Not exec: replacing this shell would throw away the EXIT trap, and the llama
# server and the containers this script started would outlive it.
"$PY" -m uvicorn app.api:app --host "$ERIS_HOST" --port "$PORT"
