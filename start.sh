#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR/pc_server"

PY_BIN="${PY_BIN:-python3.10}"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  echo "[start] Python 3.10 not found. Please install Python 3.10 or set PY_BIN to a Python 3.10 executable."
  exit 1
fi

echo "[start] Using Python: $PY_BIN"

PY_VER="$("$PY_BIN" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
if [ "$PY_VER" != "3.10" ]; then
  echo "[start] Python $PY_VER detected. This project requires Python 3.10. Set PY_BIN to Python 3.10."
  exit 1
fi

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
