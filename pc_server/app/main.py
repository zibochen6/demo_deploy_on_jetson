from __future__ import annotations

import asyncio
import shlex
import threading
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .core.config import DemoRegistry, load_registry
from .core.deploy_service import run_deploy
from .core.run_service import run_demo, stop_run
from .core.session_manager import SessionManager
from .core.ssh_client import SSHClientWrapper, SSHConfig

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class ConnectBody(BaseModel):
    ip: str
    port: int = 22
    username: str
    password: str = ""
    sudo_password: str | None = None


class SudoBody(BaseModel):
    sudo_password: str | None = None


class DeployBody(BaseModel):
    remote_dir: str | None = None
    force: bool = False


class PrecheckBody(BaseModel):
    remote_dir: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = load_registry()
    manager = SessionManager(asyncio.get_running_loop())
    app.state.registry = registry
    app.state.manager = manager
    yield
    manager.shutdown()


app = FastAPI(title="Jetson One-Click Demo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def get_registry(request: Request) -> DemoRegistry:
    return request.app.state.registry


def get_manager(request: Request) -> SessionManager:
    return request.app.state.manager


def _resolve_remote_path(remote_dir: str, path: str) -> str:
    if not path:
        return path
    p = PurePosixPath(path)
    if p.is_absolute() or not remote_dir:
        return str(p)
    return str(PurePosixPath(remote_dir) / p)


def _parse_marker(text: str) -> tuple[str | None, str | None]:
    installed_at = None
    version = None
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "installed_at":
            installed_at = value
        elif key == "version":
            version = value
    return installed_at, version


def _effective_deploy_cfg(session, demo, override_remote_dir: str | None = None) -> dict:
    deploy_cfg = dict(demo.deploy)
    if session is not None:
        overrides = session.demo_overrides.get(demo.id, {})
        if overrides.get("remote_dir"):
            deploy_cfg["remote_dir"] = overrides["remote_dir"]
    if override_remote_dir:
        deploy_cfg["remote_dir"] = override_remote_dir
    return deploy_cfg


def _effective_demo_payload(session, demo, override_remote_dir: str | None = None) -> dict:
    payload = dict(demo.raw)
    payload["deploy"] = _effective_deploy_cfg(session, demo, override_remote_dir)
    return payload


def inspect_deploy_status(session, demo, deploy_cfg: dict) -> dict:
    remote_dir = deploy_cfg.get("remote_dir", "")
    marker_path = deploy_cfg.get("marker_path", "")
    precheck_cmd = deploy_cfg.get("precheck_cmd", "")
    version_hint = deploy_cfg.get("version") or demo.raw.get("status", {}).get("version")

    if marker_path:
        marker_full = _resolve_remote_path(remote_dir, marker_path)
        cmd = f"test -f {shlex.quote(marker_full)} && cat {shlex.quote(marker_full)}"
        exit_code, stdout, _ = session.ssh.run_command(cmd)
        if exit_code == 0:
            installed_at, version = _parse_marker(stdout)
            return {
                "installed": True,
                "installed_at": installed_at,
                "version": version or version_hint,
                "method": "marker",
                "marker_path": marker_full,
            }

    if precheck_cmd:
        cmd = precheck_cmd.replace("{remote_dir}", remote_dir)
        exit_code, stdout, _ = session.ssh.run_command(cmd)
        installed = exit_code == 0
        installed_at, version = _parse_marker(stdout)
        if not installed:
            installed_at = None
            version = None
        return {
            "installed": installed,
            "installed_at": installed_at,
            "version": version or version_hint if installed else None,
            "method": "precheck",
            "marker_path": None,
        }

    return {
        "installed": False,
        "installed_at": None,
        "version": None,
        "method": "none",
        "marker_path": None,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    registry = get_registry(request)
    return TEMPLATES.TemplateResponse("index.html", {"request": request, "demos": registry.list()})


@app.get("/demo/{demo_id}", response_class=HTMLResponse)
async def demo_detail(request: Request, demo_id: str):
    registry = get_registry(request)
    demo = registry.get(demo_id)
    if demo is None:
        raise HTTPException(404, "demo not found")
    return TEMPLATES.TemplateResponse("demo_detail.html", {"request": request, "demo": demo})


@app.post("/api/session/connect")
async def connect(body: ConnectBody, request: Request):
    if not body.ip:
        raise HTTPException(400, "ip required")
    ssh = SSHClientWrapper(
        SSHConfig(
            body.ip,
            body.port,
            body.username,
            body.password,
            sudo_password=body.sudo_password,
        )
    )
    try:
        ssh.connect()
        exit_code, stdout, stderr = ssh.run_command("echo CONNECT_OK && uname -a")
        if exit_code != 0 or "CONNECT_OK" not in stdout:
            raise RuntimeError(stderr or "SSH verification failed")
    except Exception as exc:
        ssh.close()
        raise HTTPException(400, f"SSH 连接失败: {exc}")

    manager = get_manager(request)
    session = manager.create_session(ssh)
    return {"session_id": session.session_id, "message": "connected"}


@app.post("/api/session/{session_id}/sudo")
async def set_sudo_password(session_id: str, body: SudoBody, request: Request):
    manager = get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    session.ssh.config.sudo_password = body.sudo_password
    return {"message": "sudo password updated"}


@app.get("/api/session/{session_id}/demo/{demo_id}/status")
async def demo_status(session_id: str, demo_id: str, request: Request):
    registry = get_registry(request)
    demo = registry.get(demo_id)
    if demo is None:
        raise HTTPException(404, "demo not found")
    manager = get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")

    deploy_cfg = _effective_deploy_cfg(session, demo)
    status = inspect_deploy_status(session, demo, deploy_cfg)
    if status.get("installed"):
        session.deployed_demos.add(demo_id)
    return status


@app.post("/api/session/{session_id}/demo/{demo_id}/precheck")
async def demo_precheck(session_id: str, demo_id: str, body: PrecheckBody | None, request: Request):
    registry = get_registry(request)
    demo = registry.get(demo_id)
    if demo is None:
        raise HTTPException(404, "demo not found")
    manager = get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")

    override_dir = (body.remote_dir or "").strip() if body else ""
    if override_dir:
        if not override_dir.startswith("/"):
            raise HTTPException(400, "remote_dir must be absolute")
        if any(ch.isspace() for ch in override_dir):
            raise HTTPException(400, "remote_dir cannot contain spaces")
        session.demo_overrides.setdefault(demo_id, {})["remote_dir"] = override_dir
    deploy_cfg = _effective_deploy_cfg(session, demo, override_dir or None)
    status = inspect_deploy_status(session, demo, deploy_cfg)
    if status.get("installed"):
        session.deployed_demos.add(demo_id)
    status["remote_dir"] = deploy_cfg.get("remote_dir", "")
    return status


@app.post("/api/session/{session_id}/demo/{demo_id}/camera_check")
async def camera_check(session_id: str, demo_id: str, body: PrecheckBody | None, request: Request):
    registry = get_registry(request)
    demo = registry.get(demo_id)
    if demo is None:
        raise HTTPException(404, "demo not found")
    if not demo.run.get("enabled", True):
        raise HTTPException(400, "run not supported")
    manager = get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")

    override_dir = (body.remote_dir or "").strip() if body else ""
    if override_dir:
        if not override_dir.startswith("/"):
            raise HTTPException(400, "remote_dir must be absolute")
        if any(ch.isspace() for ch in override_dir):
            raise HTTPException(400, "remote_dir cannot contain spaces")
        session.demo_overrides.setdefault(demo_id, {})["remote_dir"] = override_dir

    deploy_cfg = _effective_deploy_cfg(session, demo, override_dir or None)
    remote_dir = deploy_cfg.get("remote_dir", "")
    if not remote_dir:
        raise HTTPException(400, "remote_dir not configured")

    run_cfg = demo.run
    python_rel = run_cfg.get("venv_rel_path", "")
    python_bin = _resolve_remote_path(remote_dir, python_rel) if python_rel else "python3"
    if python_rel:
        exit_code, _, _ = session.ssh.run_command(f"test -x {shlex.quote(python_bin)}")
        if exit_code != 0:
            python_bin = "python3"

    camera = run_cfg.get("default_camera", "usb")
    usb_index = int(run_cfg.get("usb_index", 0))
    csi = run_cfg.get("csi_params", {}) or {}
    width = int(csi.get("width", 1280))
    height = int(csi.get("height", 720))
    flip = int(csi.get("flip", 0))
    pipeline = (
        f"nvarguscamerasrc ! video/x-raw(memory:NVMM), width={width}, height={height}, "
        f"format=NV12, framerate=30/1 ! nvvidconv flip-method={flip} ! "
        f"video/x-raw, format=BGRx ! videoconvert ! video/x-raw, format=BGR ! appsink"
    )

    code = "\n".join(
        [
            "import sys",
            "try:",
            "    import cv2",
            "except Exception as exc:",
            "    print(f'FAIL: import cv2: {exc}')",
            "    sys.exit(1)",
            f"camera = {camera!r}",
            f"usb_index = {usb_index}",
            f"pipeline = {pipeline!r}",
            "try:",
            "    if camera == 'csi':",
            "        cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)",
            "    else:",
            "        cap = cv2.VideoCapture(usb_index)",
            "    if cap is None or not cap.isOpened():",
            "        print('FAIL: open camera')",
            "        sys.exit(2)",
            "    ok, _ = cap.read()",
            "    cap.release()",
            "    if not ok:",
            "        print('FAIL: read frame')",
            "        sys.exit(3)",
            "    print('OK')",
            "    sys.exit(0)",
            "except Exception as exc:",
            "    print(f'FAIL: {exc}')",
            "    sys.exit(4)",
        ]
    )

    inner = f"{shlex.quote(python_bin)} -c {shlex.quote(code)}"
    cmd = "bash -lc " + shlex.quote(inner)
    exit_code, stdout, stderr = session.ssh.run_command(cmd)
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    if exit_code == 0 and out.upper().startswith("OK"):
        return {"ok": True, "message": "camera ok"}
    detail = out or err or f"exit={exit_code}"
    return {"ok": False, "message": detail}


@app.post("/api/session/{session_id}/deploy/{demo_id}")
async def deploy(session_id: str, demo_id: str, request: Request, body: DeployBody | None = None):
    registry = get_registry(request)
    demo = registry.get(demo_id)
    if demo is None:
        raise HTTPException(404, "demo not found")
    manager = get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    existing = manager.get_deploy_job_by_demo(session_id, demo_id)
    if existing and existing.status in {"UPLOADING", "RUNNING", "PENDING"}:
        raise HTTPException(409, "deploy already running")

    override_dir = (body.remote_dir or "").strip() if body else ""
    if override_dir:
        if not override_dir.startswith("/"):
            raise HTTPException(400, "remote_dir must be absolute")
        if any(ch.isspace() for ch in override_dir):
            raise HTTPException(400, "remote_dir cannot contain spaces")
        session.demo_overrides.setdefault(demo_id, {})["remote_dir"] = override_dir

    deploy_cfg = _effective_deploy_cfg(session, demo, override_dir or None)
    if not (body and body.force):
        status = inspect_deploy_status(session, demo, deploy_cfg)
        if status.get("installed"):
            raise HTTPException(
                409,
                detail={
                    "message": "already installed",
                    "installed_at": status.get("installed_at"),
                    "version": status.get("version"),
                    "method": status.get("method"),
                },
            )
    elif deploy_cfg.get("marker_path"):
        marker_full = _resolve_remote_path(deploy_cfg.get("remote_dir", ""), deploy_cfg.get("marker_path", ""))
        if marker_full:
            try:
                session.ssh.run_command(f"rm -f {shlex.quote(marker_full)}", sudo=bool(deploy_cfg.get("run_as_sudo")))
            except Exception:
                pass

    demo_payload = _effective_demo_payload(session, demo, override_dir or None)
    job = manager.create_deploy_job(session_id, demo_id)
    thread = threading.Thread(target=run_deploy, args=(manager, job, session, demo_payload), daemon=True)
    thread.start()
    return {"job_id": job.job_id, "ws_url": f"/ws/jobs/{job.job_id}"}


@app.post("/api/session/{session_id}/deploy/{demo_id}/cancel")
async def deploy_cancel(session_id: str, demo_id: str, request: Request):
    manager = get_manager(request)
    job = manager.get_deploy_job_by_demo(session_id, demo_id)
    if job is None:
        return {"message": "no deploy in progress"}
    job.cancel_event.set()
    if job.channel is not None:
        try:
            job.channel.close()
        except Exception:
            pass
    return {"message": "cancelled"}


@app.websocket("/ws/jobs/{job_id}")
async def ws_jobs(websocket: WebSocket, job_id: str):
    manager = websocket.app.state.manager
    job = manager.get_deploy_job(job_id)
    if job is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    for line in job.log_buffer.list():
        await websocket.send_json({"type": "log", "data": line})
    await websocket.send_json({"type": "status", "data": job.status, "exit_code": job.exit_code})
    job.ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        job.ws_clients.discard(websocket)


@app.post("/api/session/{session_id}/run/{demo_id}")
async def run(session_id: str, demo_id: str, request: Request):
    registry = get_registry(request)
    demo = registry.get(demo_id)
    if demo is None:
        raise HTTPException(404, "demo not found")
    if not demo.run.get("enabled", True):
        raise HTTPException(400, "run not supported")
    manager = get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")

    deploy_cfg = _effective_deploy_cfg(session, demo)
    status = inspect_deploy_status(session, demo, deploy_cfg)
    if demo_id not in session.deployed_demos and not status.get("installed"):
        raise HTTPException(400, "demo not deployed")
    session.deployed_demos.add(demo_id)

    existing_run = manager.get_run_by_demo(session_id, demo_id)
    if existing_run and existing_run.status in {"STARTING", "RUNNING"}:
        raise HTTPException(409, "demo already running")

    demo_payload = _effective_demo_payload(session, demo)
    run_session = manager.create_run_session(session_id, demo_id)
    thread = threading.Thread(target=run_demo, args=(manager, run_session, session, demo_payload), daemon=True)
    thread.start()
    return {
        "run_id": run_session.run_id,
        "video_url": f"/api/session/{session_id}/video/{run_session.run_id}",
        "ws_url": f"/ws/runs/{run_session.run_id}",
    }


@app.websocket("/ws/runs/{run_id}")
async def ws_runs(websocket: WebSocket, run_id: str):
    manager = websocket.app.state.manager
    run = manager.get_run_session(run_id)
    if run is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    for line in run.log_buffer.list():
        await websocket.send_json({"type": "log", "data": line})
    info = {}
    if run.remote_port is not None:
        info["remote_port"] = run.remote_port
    if run.local_port is not None:
        info["local_port"] = run.local_port
        info["local_url"] = f"http://127.0.0.1:{run.local_port}"
    payload = {"type": "status", "data": run.status}
    if info:
        payload["info"] = info
    await websocket.send_json(payload)
    run.ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        run.ws_clients.discard(websocket)


@app.get("/api/session/{session_id}/video/{run_id}")
async def video_proxy(session_id: str, run_id: str, request: Request):
    manager = get_manager(request)
    run = manager.get_run_session(run_id)
    if run is None or run.local_port is None:
        raise HTTPException(404, "run not found")
    if run.session_id != session_id:
        raise HTTPException(403, "forbidden")

    url = f"http://127.0.0.1:{run.local_port}/video"

    async def stream():
        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            try:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            except (
                httpx.RemoteProtocolError,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.StreamError,
            ):
                return
            except asyncio.CancelledError:
                return

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.post("/api/session/{session_id}/stop/{run_id}")
async def stop(session_id: str, run_id: str, request: Request):
    manager = get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    run_session = manager.get_run_session(run_id)
    if run_session is None:
        raise HTTPException(404, "run not found")
    if run_session.session_id != session_id:
        raise HTTPException(403, "forbidden")
    stop_run(manager, run_session, session)
    return {"message": "stopped"}
