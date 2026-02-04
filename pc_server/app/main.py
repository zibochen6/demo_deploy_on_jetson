from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path

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


def is_deployed(session, demo) -> bool:
    deploy_cfg = demo.deploy
    run_cfg = demo.run
    remote_dir = deploy_cfg.get("remote_dir", "")
    venv_rel = run_cfg.get("venv_rel_path", "")
    model_hint = run_cfg.get("model_hint", "yolo11n.pt")
    venv_path = f"{remote_dir.rstrip('/')}/{venv_rel}"
    model_path = f"{remote_dir.rstrip('/')}/yolo11/{model_hint}"
    cmd = f"test -f '{venv_path}' && test -f '{model_path}' && echo ok"
    exit_code, stdout, _ = session.ssh.run_command(cmd)
    return exit_code == 0 and "ok" in stdout


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

    deployed = is_deployed(session, demo)
    if deployed:
        session.deployed_demos.add(demo_id)
    return {"deployed": deployed}


@app.post("/api/session/{session_id}/deploy/{demo_id}")
async def deploy(session_id: str, demo_id: str, request: Request):
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

    job = manager.create_deploy_job(session_id, demo_id)
    thread = threading.Thread(target=run_deploy, args=(manager, job, session, demo.raw), daemon=True)
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
    manager = get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")

    if demo_id not in session.deployed_demos and not is_deployed(session, demo):
        raise HTTPException(400, "demo not deployed")
    session.deployed_demos.add(demo_id)

    existing_run = manager.get_run_by_demo(session_id, demo_id)
    if existing_run and existing_run.status in {"STARTING", "RUNNING"}:
        raise HTTPException(409, "demo already running")

    run_session = manager.create_run_session(session_id, demo_id)
    thread = threading.Thread(target=run_demo, args=(manager, run_session, session, demo.raw), daemon=True)
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
    await websocket.send_json({"type": "status", "data": run.status})
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
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

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
