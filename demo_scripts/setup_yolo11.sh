#!/usr/bin/env bash
set -euo pipefail

WORK_DIR="$(pwd)"
YOLO_DIR="$WORK_DIR/yolo11"
VENV_DIR="$YOLO_DIR/.venv"
MODEL_URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt"
MODEL_PATH="$YOLO_DIR/yolo11n.pt"

echo "[setup] Working dir: $WORK_DIR"
mkdir -p "$YOLO_DIR"

if command -v apt-get >/dev/null 2>&1; then
  echo "[setup] Installing system dependencies..."
  apt-get update -y
  apt-get install -y python3-venv python3-opencv curl
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[setup] Installing uv..."
  python3 -m pip install --user uv
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "[setup] Creating venv with uv..."
  uv venv --python=python3 --system-site-packages "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip

echo "[setup] Installing PyTorch + torchvision for JetPack 6.2..."
python -m pip install --extra-index-url https://developer.download.nvidia.com/compute/redist/jp/v62 torch torchvision

echo "[setup] Installing ultralytics + fastapi + uvicorn..."
python -m pip install ultralytics fastapi "uvicorn[standard]"

if [ ! -f "$MODEL_PATH" ]; then
  echo "[setup] Downloading YOLO model..."
  curl -L "$MODEL_URL" -o "$MODEL_PATH"
fi

echo "[setup] Done. Venv: $VENV_DIR"
