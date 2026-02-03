"""
FastAPI：支持双模式（本地 / 远程 Jetson）。
无 current_target 时为本机 subprocess；有 current_target 时通过 SSH 在 Jetson 上执行部署与推流。
"""
import asyncio
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import paramiko
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from demos import (
    DEFAULT_JETSON_PROJECT,
    get_demo,
    is_deployed_local,
    jetson_paths,
    list_demos,
    local_model_path,
    local_script_path,
    local_venv_path,
    local_work_dir,
    PROJECT_ROOT,
)

app = FastAPI(title="Jetson Demo Web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 当前连接的 Jetson（PC 端部署时使用）；None 表示本地模式
# 结构: { "client": paramiko.SSHClient, "host": str, "port": int, "username": str, "jetson_project": str }
_current_target: dict | None = None

# 会话：session_id -> current_target，用于多会话时按 token 区分（可选扩展）
_sessions: dict[str, dict] = {}

# 部署状态：demo_id -> { "process" | "channel", "log_queue", "thread" }
_deploy_state: dict[str, dict] = {}
_stream_processes: dict[str, subprocess.Popen] = {}
_stream_channels: dict[str, paramiko.Channel] = {}
_last_stream_error: dict[str, str] = {}
MJPEG_FIRST_FRAME_TIMEOUT = 45


class ConnectBody(BaseModel):
    host: str
    port: int = 22
    username: str = "seeed"
    password: str | None = None
    jetson_project_path: str | None = None


class DeployBody(BaseModel):
    password: str = ""


def _get_target(request: Request = None):
    """从 Header X-Session-Token 或全局 current_target 获取目标。请求未传时用全局。"""
    if request:
        token = request.headers.get("X-Session-Token", "").strip()
        if token and token in _sessions:
            return _sessions[token]
    return _current_target


# ---------- 连接 API ----------


@app.post("/api/connect")
async def api_connect(body: ConnectBody):
    """连接 Jetson：SSH 连接成功后保存为当前目标，后续部署/推流在该设备上执行。"""
    global _current_target
    host = (body.host or "").strip()
    if not host:
        raise HTTPException(400, "host 不能为空")
    port = body.port or 22
    username = (body.username or "seeed").strip()
    password = (body.password or "").strip() or None
    jetson_project = (body.jetson_project_path or DEFAULT_JETSON_PROJECT).rstrip("/")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=username, password=password, timeout=15)
    except Exception as e:
        raise HTTPException(400, f"SSH 连接失败: {e}")
    if _current_target and _current_target.get("client"):
        try:
            _current_target["client"].close()
        except Exception:
            pass
    _current_target = {
        "client": client,
        "host": host,
        "port": port,
        "username": username,
        "jetson_project": jetson_project,
    }
    session_id = str(uuid.uuid4())
    _sessions[session_id] = _current_target
    return {"connected": True, "host": host, "session_id": session_id}


@app.get("/api/connect/status")
async def api_connect_status(request: Request):
    """返回当前是否已连接 Jetson。"""
    target = _get_target(request)
    if target:
        return {"connected": True, "host": target.get("host", "")}
    return {"connected": False}


# ---------- 静态页 ----------


@app.get("/", response_class=HTMLResponse)
async def index():
    p = STATIC_DIR / "index.html"
    if not p.is_file():
        raise HTTPException(404, "index.html not found")
    return p.read_text(encoding="utf-8")


@app.get("/demo", response_class=HTMLResponse)
async def demo_page():
    p = STATIC_DIR / "demo.html"
    if not p.is_file():
        raise HTTPException(404, "demo.html not found")
    return p.read_text(encoding="utf-8")


# ---------- Demo 列表与状态 ----------


@app.get("/api/demos")
async def api_list_demos():
    return {"demos": list_demos()}


def _is_deployed_remote(target: dict, demo_id: str) -> bool:
    """通过 SSH 检查 Jetson 上是否已部署。"""
    client = target.get("client")
    if not client:
        return False
    paths = jetson_paths(target["jetson_project"], demo_id)
    venv = paths.get("venv_python", "")
    model = paths.get("model_path", "")
    if not venv or not model:
        return False
    cmd = f"test -f '{venv}' && test -f '{model}' && echo ok"
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=5)
        out = stdout.read().decode("utf-8", errors="replace").strip()
        return out == "ok"
    except Exception:
        return False


@app.get("/api/demos/{demo_id}/status")
async def api_demo_status(demo_id: str, request: Request):
    if get_demo(demo_id) is None:
        raise HTTPException(404, "demo not found")
    target = _get_target(request)
    if target:
        deployed = _is_deployed_remote(target, demo_id)
    else:
        deployed = is_deployed_local(demo_id)
    return {"deployed": deployed}


# ---------- 一键部署（本地 or SSH）----------


def _read_deploy_output_process(process: subprocess.Popen, log_queue: queue.Queue) -> None:
    try:
        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                log_queue.put(line)
    finally:
        process.wait()
        log_queue.put(None)


def _read_deploy_output_ssh(channel, log_queue: queue.Queue) -> None:
    try:
        while not channel.exit_status_ready():
            if channel.recv_ready():
                data = channel.recv(4096).decode("utf-8", errors="replace")
                for line in data.splitlines(keepends=True):
                    log_queue.put(line)
            if channel.recv_stderr_ready():
                channel.recv_stderr(4096)
        while channel.recv_ready():
            data = channel.recv(4096).decode("utf-8", errors="replace")
            for line in data.splitlines(keepends=True):
                log_queue.put(line)
    except Exception:
        pass
    finally:
        log_queue.put(None)


@app.post("/api/demos/{demo_id}/deploy")
async def api_start_deploy(demo_id: str, request: Request, body: DeployBody | None = None):
    if get_demo(demo_id) is None:
        raise HTTPException(404, "demo not found")
    if _deploy_state.get(demo_id):
        raise HTTPException(409, "deploy already in progress")
    target = _get_target(request)
    password = (body.password or "").strip() if body else ""

    if target:
        paths = jetson_paths(target["jetson_project"], demo_id)
        script_path = paths.get("script_path", "")
        work_dir = paths.get("work_dir", "")
        if not script_path or not work_dir:
            raise HTTPException(500, "jetson paths not configured")
        client = target["client"]
        cmd = f"cd '{work_dir}' && bash '{script_path}'"
        try:
            channel = client.get_transport().open_session()
            channel.exec_command(cmd)
        except Exception as e:
            raise HTTPException(500, f"SSH exec failed: {e}")
        log_queue = queue.Queue()
        thread = threading.Thread(target=_read_deploy_output_ssh, args=(channel, log_queue), daemon=True)
        thread.start()
        _deploy_state[demo_id] = {"channel": channel, "log_queue": log_queue, "thread": thread}
    else:
        script_path = local_script_path(demo_id)
        work_dir = local_work_dir(demo_id)
        if not script_path or not os.path.isfile(script_path):
            raise HTTPException(500, f"script not found: {script_path}")
        os.chmod(script_path, 0o755)
        deploy_env = os.environ.copy()
        local_bin = os.path.expanduser("~/.local/bin")
        if local_bin and local_bin not in deploy_env.get("PATH", ""):
            deploy_env["PATH"] = local_bin + os.pathsep + deploy_env.get("PATH", "")
        cmd = [os.path.abspath(script_path)]
        if password:
            cmd = ["sudo", "-S"] + cmd
        proc = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            stdin=subprocess.PIPE if password else subprocess.DEVNULL,
            env=deploy_env,
        )
        if password:
            proc.stdin.write(password + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        log_queue = queue.Queue()
        thread = threading.Thread(target=_read_deploy_output_process, args=(proc, log_queue), daemon=True)
        thread.start()
        _deploy_state[demo_id] = {"process": proc, "log_queue": log_queue, "thread": thread}

    return {"stream_url": f"/api/demos/{demo_id}/deploy/stream"}


async def _deploy_stream_events(demo_id: str):
    """SSE 事件流：部署日志，结束时发送 event: done。"""
    state = _deploy_state.get(demo_id)
    if not state:
        _deploy_state.pop(demo_id, None)
        yield "event: error\ndata: no deploy in progress\n\n"
        return
    log_queue = state["log_queue"]
    channel = state.get("channel")
    process = state.get("process")
    try:
        while True:
            try:
                line = log_queue.get_nowait()
            except queue.Empty:
                if channel and channel.exit_status_ready():
                    break
                if process is not None and process.poll() is not None:
                    break
                await asyncio.sleep(0.2)
                continue
            if line is None:
                break
            data = line.rstrip("\n").replace("\n", "\ndata: ")
            yield f"data: {data}\n\n"
        exit_code = 0
        if channel:
            try:
                exit_code = channel.recv_exit_status()
            except Exception:
                exit_code = -1
        elif process is not None:
            exit_code = process.returncode or 0
        yield f"event: done\ndata: {{\"exit_code\": {exit_code}}}\n\n"
    finally:
        _deploy_state.pop(demo_id, None)


@app.get("/api/demos/{demo_id}/deploy/stream")
async def api_deploy_stream(demo_id: str):
    return StreamingResponse(
        _deploy_stream_events(demo_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/demos/{demo_id}/deploy/cancel")
async def api_deploy_cancel(demo_id: str):
    """清除卡住的部署状态，便于在 409 后重试。"""
    state = _deploy_state.pop(demo_id, None)
    if not state:
        return {"ok": True, "message": "no deploy in progress"}
    channel = state.get("channel")
    process = state.get("process")
    if channel:
        try:
            channel.close()
        except Exception:
            pass
    if process is not None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    return {"ok": True, "message": "deploy cancelled"}


# ---------- 推流（本地 or SSH）----------


@app.get("/api/demos/{demo_id}/stream-last-error")
async def api_stream_last_error(demo_id: str):
    if get_demo(demo_id) is None:
        raise HTTPException(404, "demo not found")
    return {"stderr": _last_stream_error.get(demo_id, ""), "detail": ""}


def _stream_yolo_reader_local(proc: subprocess.Popen, frame_queue: queue.Queue) -> None:
    try:
        while proc.stdout:
            raw = proc.stdout.read(4)
            if len(raw) < 4:
                break
            (size,) = struct.unpack(">I", raw)
            if size <= 0 or size > 10 * 1024 * 1024:
                break
            jpeg = proc.stdout.read(size)
            if len(jpeg) < size:
                break
            frame_queue.put(jpeg)
    except Exception:
        pass
    finally:
        frame_queue.put(None)
    proc.wait()


def _stream_yolo_reader_ssh(channel, frame_queue: queue.Queue) -> None:
    try:
        buf = b""
        while not channel.exit_status_ready() or channel.recv_ready():
            if channel.recv_ready():
                buf += channel.recv(65536)
            while len(buf) >= 4:
                (size,) = struct.unpack(">I", buf[:4])
                if size <= 0 or size > 10 * 1024 * 1024:
                    buf = buf[4:]
                    continue
                if len(buf) < 4 + size:
                    break
                jpeg = buf[4 : 4 + size]
                buf = buf[4 + size :]
                frame_queue.put(jpeg)
            if not channel.recv_ready():
                break
    except Exception:
        pass
    finally:
        frame_queue.put(None)


def _wait_first_frame(frame_queue: queue.Queue, timeout: float):
    return frame_queue.get(timeout=timeout)


def _read_stderr(proc: subprocess.Popen) -> str:
    if not getattr(proc, "stderr", None):
        return ""
    try:
        return proc.stderr.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


@app.get("/api/demos/{demo_id}/stream")
async def api_demo_stream(demo_id: str, request: Request):
    import logging
    log = logging.getLogger("uvicorn.error")
    if get_demo(demo_id) is None:
        raise HTTPException(404, "demo not found")
    target = _get_target(request)
    if target:
        deployed = _is_deployed_remote(target, demo_id)
    else:
        deployed = is_deployed_local(demo_id)
    if not deployed:
        msg = "demo not deployed yet"
        _last_stream_error[demo_id] = "请先完成一键部署，再运行 Demo。"
        log.warning("stream 503: %s", msg)
        raise HTTPException(503, msg)
    if _stream_processes.get(demo_id) or _stream_channels.get(demo_id):
        raise HTTPException(409, "stream already in use")

    paths = jetson_paths(target["jetson_project"], demo_id) if target else None
    venv_python = paths["venv_python"] if paths else local_venv_path(demo_id)
    model_path = paths["model_path"] if paths else local_model_path(demo_id)
    yolo_dir = paths["yolo_dir"] if paths else os.path.join(PROJECT_ROOT, "yolo11")
    stream_script = paths["stream_script"] if paths else str(Path(__file__).resolve().parent / "stream_yolo.py")

    boundary = b"frame"
    env = os.environ.copy()
    env["YOLO11_PROJECT_DIR"] = yolo_dir
    env["YOLO11_MODEL_PATH"] = model_path
    env["PYTHONUNBUFFERED"] = "1"

    if target:
        client = target["client"]
        cmd = f"cd '{yolo_dir}' && YOLO11_PROJECT_DIR='{yolo_dir}' YOLO11_MODEL_PATH='{model_path}' PYTHONUNBUFFERED=1 '{venv_python}' '{stream_script}'"
        try:
            channel = client.get_transport().open_session()
            channel.exec_command(cmd)
        except Exception as e:
            raise HTTPException(500, f"SSH exec stream failed: {e}")
        _stream_channels[demo_id] = channel
        frame_queue = queue.Queue()
        thread = threading.Thread(target=_stream_yolo_reader_ssh, args=(channel, frame_queue), daemon=True)
        thread.start()
    else:
        proc = subprocess.Popen(
            [venv_python, stream_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=yolo_dir,
            env=env,
        )
        _stream_processes[demo_id] = proc
        frame_queue = queue.Queue()
        thread = threading.Thread(target=_stream_yolo_reader_local, args=(proc, frame_queue), daemon=True)
        thread.start()

    first_frame_timeout = MJPEG_FIRST_FRAME_TIMEOUT
    if target:
        first_frame_timeout = 60
    loop = asyncio.get_event_loop()
    try:
        first_frame = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _wait_first_frame(frame_queue, first_frame_timeout)),
            timeout=first_frame_timeout + 10,
        )
    except (asyncio.TimeoutError, queue.Empty):
        if target and demo_id in _stream_channels:
            try:
                _stream_channels[demo_id].close()
            except Exception:
                pass
            _stream_channels.pop(demo_id, None)
        if not target and demo_id in _stream_processes:
            proc = _stream_processes[demo_id]
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            _stream_processes.pop(demo_id, None)
        err_msg = "拉流超时：模型或摄像头未在限定时间内就绪。请确认 Jetson 上已部署、摄像头已连接且未被占用。"
        _last_stream_error[demo_id] = err_msg
        log.warning("stream 503: %s", err_msg)
        raise HTTPException(503, err_msg)
    if first_frame is None:
        stderr_text = ""
        if target and demo_id in _stream_channels:
            ch = _stream_channels.get(demo_id)
            if ch:
                try:
                    if ch.recv_stderr_ready():
                        stderr_text = ch.recv_stderr(8192).decode("utf-8", errors="replace").strip()
                    ch.close()
                except Exception:
                    pass
            _stream_channels.pop(demo_id, None)
        if not target and demo_id in _stream_processes:
            proc = _stream_processes[demo_id]
            try:
                stderr_text = proc.stderr.read().decode("utf-8", errors="replace").strip() if proc.stderr else ""
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            _stream_processes.pop(demo_id, None)
        err_msg = "stream_yolo 进程异常退出（未产出首帧）。请查看 Jetson 上摄像头与模型是否正常。"
        if stderr_text:
            err_msg = err_msg + "\n\n进程 stderr:\n" + stderr_text[:1500]
        _last_stream_error[demo_id] = err_msg
        log.warning("stream 503: %s", err_msg[:300])
        raise HTTPException(503, err_msg)

    def generate():
        nonlocal first_frame, frame_queue, demo_id
        try:
            yield b"--" + boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + first_frame + b"\r\n"
            while True:
                jpeg = frame_queue.get()
                if jpeg is None:
                    break
                yield b"--" + boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        finally:
            if demo_id in _stream_channels:
                try:
                    _stream_channels[demo_id].close()
                except Exception:
                    pass
                _stream_channels.pop(demo_id, None)
            if demo_id in _stream_processes:
                proc = _stream_processes[demo_id]
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _stream_processes.pop(demo_id, None)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache"},
    )


def _shutdown_and_exit(*_args):
    """Ctrl+C / kill 时关闭 SSH、结束子进程并退出，便于释放端口。"""
    global _current_target, _deploy_state, _stream_processes, _stream_channels
    if _current_target and _current_target.get("client"):
        try:
            _current_target["client"].close()
        except Exception:
            pass
        _current_target = None
    for demo_id, state in list(_deploy_state.items()):
        ch = state.get("channel")
        proc = state.get("process")
        if ch:
            try:
                ch.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    _deploy_state.clear()
    for demo_id, proc in list(_stream_processes.items()):
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _stream_processes.clear()
    for demo_id, ch in list(_stream_channels.items()):
        try:
            ch.close()
        except Exception:
            pass
    _stream_channels.clear()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown_and_exit)
    signal.signal(signal.SIGTERM, _shutdown_and_exit)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
