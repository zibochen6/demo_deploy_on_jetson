#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR/pc_server"

PY_BIN="${PY_BIN:-python3.10}"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  if command -v python3.11 >/dev/null 2>&1; then
    PY_BIN=python3.11
  elif command -v python3.12 >/dev/null 2>&1; then
    PY_BIN=python3.12
  elif command -v python3 >/dev/null 2>&1; then
    PY_BIN=python3
  fi
fi

echo "[start] Using Python: $PY_BIN"

if [ ! -d .venv ]; then
  "$PY_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -r requirements.txt

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo "[start] Serving on http://$HOST:$PORT"
exec uvicorn app.main:app --host "$HOST" --port "$PORT"
