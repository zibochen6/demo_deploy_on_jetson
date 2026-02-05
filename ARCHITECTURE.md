# System Architecture

This document describes the overall architecture of **demo_deploy_on_jetson**, including major components, data flows, and deployment/runtime interactions. It also includes a diagram to help visualize the system.

## Overview
The system is a **PC-hosted web platform** that connects to a Jetson device via SSH to:
1. Deploy demo dependencies and assets.
2. Launch a remote inference service.
3. Stream logs and MJPEG video back to the PC browser.

Key ideas:
- The PC runs a FastAPI server and serves the web UI.
- The PC uses SSH to control and transfer files to the Jetson.
- Logs and status updates are pushed to the browser via WebSocket.
- Video is proxied from Jetson to the browser through an SSH tunnel.

## Architecture Diagram (Bash Rendered)
```bash
cat <<'EOF'
+-----------------------------------------------------------------------------------+
|                              PC Host (Windows/Linux)                              |
|                                                                                   |
|  [Web UI] <--HTTP--> [FastAPI Server] <---> [SessionManager] <---> [demos.json]   |
|     |                        |                     |                              |
|     |                        +-- WebSocket (logs/status) --> [Web UI]             |
|     |                        |                                                    |
|     |                        +-- Deploy Service --SSH/SFTP--> [SSH Server]        |
|     |                        |                                                    |
|     |                        +-- Run Service ----SSH/SFTP--> [SSH Server]         |
|     |                        |                                                    |
|     |                        +-- SSH Tunnel (local->remote) <----+                |
|     |                                                           |                |
|     +-------------------- MJPEG /video -------------------------+                |
+-----------------------------------------------------------------------------------+
                                        |
                                        | SSH / SFTP
                                        v
+-----------------------------------------------------------------------------------+
|                                   Jetson Device                                   |
|                                                                                   |
|  [SSH Server] -> [Remote Files /tmp/oneclick_demos/*] -> [Deploy Script setup_*.sh]|
|                                       |                                            |
|                                       v                                            |
|                          [Runtime Env: Python/Venv/Ollama/etc]                     |
|                                       |                                            |
|                                       v                                            |
|                  [Inference Service yolo_stream_server.py / WebUI]                |
|                                       |                                            |
|                                       v                                            |
|                                   [Camera Device]                                 |
|                                       ^                                            |
|                                       +-- /health + /video (via tunnel)           |
+-----------------------------------------------------------------------------------+
EOF
```

## Component Responsibilities
### Web UI (`pc_server/app/templates`, `pc_server/app/static`)
- Connects to Jetson.
- Triggers deploy, run, stop, camera check.
- Shows status badges and streaming logs.
- Displays MJPEG video via `/api/session/{id}/video/{run_id}`.

### FastAPI Server (`pc_server/app/main.py`)
- Exposes REST APIs for connect, deploy, run, stop, precheck, and camera check.
- Serves HTML/CSS/JS assets.
- Streams video by proxying the tunnel endpoint.
- Manages WebSocket endpoints for deploy/run logs.

### Session Manager (`pc_server/app/core/session_manager.py`)
- Tracks sessions, deploy jobs, and run sessions.
- Buffers logs for reconnects.
- Broadcasts status/log updates to WebSocket clients.

### SSH Client (`pc_server/app/core/ssh_client.py`)
- Wraps Paramiko for SSH execution and SFTP transfers.
- Supports sudo execution for setup tasks.

### Deploy Service (`pc_server/app/core/deploy_service.py`)
- Downloads or loads the deployment script.
- Uploads script to Jetson and executes it.
- Streams stdout/stderr back to the UI.
- Writes install markers for precheck.

### Run Service (`pc_server/app/core/run_service.py`)
- Uploads runtime payload (e.g., `yolo_stream_server.py`).
- Starts remote inference service.
- Creates SSH tunnel to remote port.
- Checks `/health` and reports readiness.
- Cleans up remote processes/ports on stop.

### Jetson Payload (`jetson_payload/`)
- Minimal runtime server for streaming inference results.
- Example: `yolo_stream_server.py` exposes `/health` and `/video`.

### Demo Configuration (`demos.json`)
- Defines deploy/run behavior per demo.
- Controls script locations, remote directories, runtime options.

## End-to-End Flow
1. **Connect**
   - UI sends `/api/session/connect`.
   - Server establishes SSH connection and stores session.
2. **Deploy**
   - UI triggers `/api/session/{id}/deploy/{demo}`.
   - Deploy script is uploaded and executed on Jetson.
   - Logs are streamed over WebSocket.
3. **Run**
   - UI triggers `/api/session/{id}/run/{demo}`.
   - Payload is uploaded, service started, tunnel created.
   - `/health` is polled to confirm readiness.
   - `/video` is proxied to the UI.
4. **Stop**
   - UI triggers `/api/session/{id}/stop/{run}`.
   - Remote process and related ports are cleaned up.

## Runtime Notes
- Logs are sanitized to avoid ANSI/control character noise.
- Port conflicts are handled automatically by killing stale listeners or switching ports.
- Session reconnects handle precheck and log buffers.

## Key Files
- `pc_server/app/main.py`
- `pc_server/app/core/deploy_service.py`
- `pc_server/app/core/run_service.py`
- `pc_server/app/core/session_manager.py`
- `pc_server/app/core/ssh_client.py`
- `jetson_payload/yolo_stream_server.py`
- `demos.json`
