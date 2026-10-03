#!/usr/bin/env bash
set -euo pipefail

# Start a local llama.cpp OpenAI-compatible server.
# Requires llama.cpp built with a FILKAN backend (or any backend).
MODEL="${1:-./models/model.gguf}"
PORT="${PORT:-8080}"

exec llama-server \
  --model "$MODEL" \
  --port "$PORT" \
  --host 0.0.0.0 \
  --ctx-size 8192 \
  --embeddings \
  --embedding \
  --pooling mean \
  --n-gpu-layers 99 \
  --reasoning-format deepseek