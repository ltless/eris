#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env
set -a; source .env; set +a

PORT="${PORT:-9000}"
MODEL_PATH="${MODEL_PATH:-}"
LLAMA_BIN="${LLAMA_BIN:-llama-server}"

started_docker=0
cleanup() {
  [ -n "${llama_pid:-}" ] && kill "$llama_pid" 2>/dev/null || true
  [ "$started_docker" = 1 ] && docker compose stop >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# 1. venv + deps
[ -d .venv ] || { echo "> creating venv"; python3 -m venv .venv; }
PY=.venv/bin/python
.venv/bin/pip install -q -r requirements.txt

# 2. SearXNG
if command -v docker >/dev/null; then
  echo "> starting docker services (searxng)"
  docker compose up -d
  started_docker=1
else
  echo "! docker not found, skipping SearXNG"
fi

# 3. llama.cpp model server
if [ -n "$MODEL_PATH" ] && [ -f "$MODEL_PATH" ]; then
  if command -v "$LLAMA_BIN" >/dev/null; then
    echo "> starting $LLAMA_BIN ($MODEL_PATH)"
    "$LLAMA_BIN" --model "$MODEL_PATH" --host 0.0.0.0 --port 8080 \
      --ctx-size 8192 \
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
exec "$PY" -m uvicorn app.api:app --host 0.0.0.0 --port "$PORT"