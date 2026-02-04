#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() {
  echo "[live-vlm-webui] $*"
}

resolve_target_user() {
  local user="${SUDO_USER:-}"
  if [ -z "$user" ]; then
    user="$(logname 2>/dev/null || true)"
  fi
  if [ -z "$user" ] || ! id -u "$user" >/dev/null 2>&1; then
    user="root"
  fi
  echo "$user"
}

run_as_user() {
  local user="$1"
  shift
  if [ "$user" = "root" ]; then
    "$@"
  elif command -v runuser >/dev/null 2>&1; then
    runuser -u "$user" -- "$@"
  else
    sudo -u "$user" -H "$@"
  fi
}

TARGET_USER="$(resolve_target_user)"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
if [ -z "$TARGET_HOME" ]; then
  TARGET_HOME="$HOME"
fi

log "Checking base tools..."
if ! command -v curl >/dev/null 2>&1; then
  log "Installing curl..."
  apt-get update
  apt-get install -y curl
fi

if ! command -v python3 >/dev/null 2>&1; then
  log "Installing python3..."
  apt-get update
  apt-get install -y python3
fi

log "Installing Ollama (if missing)..."
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
else
  log "Ollama already installed."
fi

if command -v systemctl >/dev/null 2>&1; then
  if ! systemctl is-active --quiet ollama; then
    log "Starting Ollama service..."
    systemctl start ollama || true
  fi
fi

log "Pulling model llama3.2-vision:11b (if missing)..."
if ollama list 2>/dev/null | grep -q "llama3.2-vision:11b"; then
  log "Model already present."
else
  ollama pull llama3.2-vision:11b
fi

log "Installing dependencies..."
apt-get update
apt-get install -y openssl python3-pip

log "Installing live-vlm-webui..."
if ! run_as_user "$TARGET_USER" python3 -m pip install --user -U live-vlm-webui; then
  run_as_user "$TARGET_USER" python3 -m pip install --user -U --break-system-packages live-vlm-webui
fi

if [ "$TARGET_USER" != "root" ]; then
  if ! grep -q 'HOME/.local/bin' "$TARGET_HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$TARGET_HOME/.bashrc"
  fi
fi

log "Done. You can run: live-vlm-webui --host 0.0.0.0 --port 8090"
