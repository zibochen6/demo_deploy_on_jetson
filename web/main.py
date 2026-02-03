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


class StreamUploadPasswordBody(BaseModel):
    """Jetson 写权限密码（SFTP 无写权限时用于 sudo 写入 stream_yolo.py）。"""
    password: str | None = None


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


@app.post("/api/connect/stream-upload-password")
async def api_stream_upload_password(request: Request, body: StreamUploadPasswordBody):
    """保存 Jetson 写权限密码（SFTP 无写权限时用于 sudo 写入 stream_yolo.py）。仅远程模式生效。"""
    target = _get_target(request)
    if not target:
        raise HTTPException(400, "请先连接 Jetson")
    target["stream_upload_password"] = (body.password or "").strip() or None
    return {"ok": True, "message": "已保存" if target.get("stream_upload_password") else "已清除"}


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


@app.get("/api/demos/{demo_id}/stream-debug")
async def api_stream_debug(demo_id: str, request: Request):
    """调试用：返回最近错误、路径与可在 Jetson 上手动执行的命令预览。"""
    if get_demo(demo_id) is None:
        raise HTTPException(404, "demo not found")
    target = _get_target(request)
    paths = jetson_paths(target["jetson_project"], demo_id) if target else None
    yolo_dir = paths["yolo_dir"] if paths else os.path.join(PROJECT_ROOT, "yolo11")
    venv_python = paths["venv_python"] if paths else local_venv_path(demo_id)
    model_path = paths["model_path"] if paths else local_model_path(demo_id)
    stream_script = (os.path.join(yolo_dir, "stream_yolo.py") if target else str(Path(__file__).resolve().parent / "stream_yolo.py"))
    cmd_preview = (
        f"cd '{yolo_dir}' && "
        f"YOLO11_PROJECT_DIR='{yolo_dir}' YOLO11_MODEL_PATH='{model_path}' PYTHONUNBUFFERED=1 "
        f"'{venv_python}' '{stream_script}'"
    )
    return {
        "last_error": _last_stream_error.get(demo_id, ""),
        "remote": target is not None,
        "paths": {
            "yolo_dir": yolo_dir,
            "venv_python": venv_python,
            "model_path": model_path,
            "stream_script": stream_script,
        },
        "command_preview": cmd_preview,
    }


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
    except Exception as e:
        _stream_log().warning("stream_yolo_reader_ssh exception: %s", e)
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


def _stream_log():
    import logging
    return logging.getLogger("uvicorn.error")


def _upload_stream_script_via_sudo(client, remote_path: str, local_path: str, password: str) -> bool:
    """通过 SSH 执行 sudo tee 将本地文件写入 Jetson（stdin 传内容）。成功返回 True。"""
    try:
        with open(local_path, "rb") as f:
            content = f.read()
    except Exception:
        return False
    # 路径中单引号需转义：' -> '\''
    escaped = remote_path.replace("'", "'\"'\"'")
    ch = client.get_transport().open_session()
    ch.exec_command(f"sudo -S tee '{escaped}' > /dev/null")
    ch.send((password + "\n").encode("utf-8"))
    ch.send(content)
    ch.shutdown_write()
    while not ch.exit_status_ready():
        if ch.recv_ready():
            ch.recv(65536)
        if ch.recv_stderr_ready():
            ch.recv_stderr(65536)
    while ch.recv_ready():
        ch.recv(65536)
    while ch.recv_stderr_ready():
        ch.recv_stderr(65536)
    status = ch.recv_exit_status()
    ch.close()
    return status == 0


@app.get("/api/demos/{demo_id}/stream")
async def api_demo_stream(demo_id: str, request: Request):
    log = _stream_log()
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
    local_stream_script = str(Path(__file__).resolve().parent / "stream_yolo.py")
    stream_script = paths["stream_script"] if paths else local_stream_script

    mode = "remote" if target else "local"
    log.info("stream start demo_id=%s mode=%s yolo_dir=%s venv=%s model=%s", demo_id, mode, yolo_dir, venv_python, model_path)

    boundary = b"frame"
    env = os.environ.copy()
    env["YOLO11_PROJECT_DIR"] = yolo_dir
    env["YOLO11_MODEL_PATH"] = model_path
    env["PYTHONUNBUFFERED"] = "1"

    if target:
        client = target["client"]
        # 远程模式：先把 stream_yolo.py 上传到 Jetson 的 yolo_dir，避免依赖 Jetson 上是否有 web 目录
        remote_stream_script = os.path.join(yolo_dir, "stream_yolo.py")
        upload_ok = False
        try:
            sftp = client.open_sftp()
            sftp.put(local_stream_script, remote_stream_script)
            sftp.close()
            upload_ok = True
            log.info("stream SFTP upload ok -> %s", remote_stream_script)
        except Exception as e:
            err_upload = f"上传 stream_yolo.py 到 Jetson 失败: {e}"
            _last_stream_error[demo_id] = err_upload
            log.warning("stream SFTP failed: %s, trying fallback", e)
            # 回退：若 Jetson 上存在 web/stream_yolo.py 则直接用
            fallback_script = paths["stream_script"]
            try:
                ch_test = client.get_transport().open_session()
                ch_test.exec_command(f"test -r '{fallback_script}' && echo ok")
                out = b""
                while ch_test.recv_ready():
                    out += ch_test.recv(4096)
                ch_test.close()
                if b"ok" in out:
                    remote_stream_script = fallback_script
                    upload_ok = True
                    log.info("stream using fallback script: %s", fallback_script)
            except Exception as e2:
                log.warning("stream fallback check failed: %s", e2)
            # 若为权限错误且已配置写权限密码，尝试通过 sudo tee 写入
            if not upload_ok and ("Permission denied" in str(e) or "Errno 13" in str(e)) and target.get("stream_upload_password"):
                try:
                    if _upload_stream_script_via_sudo(client, remote_stream_script, local_stream_script, target["stream_upload_password"]):
                        upload_ok = True
                        log.info("stream upload ok via sudo tee -> %s", remote_stream_script)
                    else:
                        _last_stream_error[demo_id] = err_upload + "；sudo 写入失败（请检查密码或 sudo 权限）。"
                except Exception as e3:
                    log.warning("stream sudo-tee upload failed: %s", e3)
                    _last_stream_error[demo_id] = err_upload + f"；sudo 写入异常: {e3}"
            if not upload_ok:
                full_msg = err_upload + "；请确认 Jetson 已开启 SFTP、存在 web/stream_yolo.py，或填写「Jetson 写权限密码」后重试。"
                _last_stream_error[demo_id] = full_msg
                raise HTTPException(500, full_msg)
        if upload_ok:
            cmd = f"cd '{yolo_dir}' && YOLO11_PROJECT_DIR='{yolo_dir}' YOLO11_MODEL_PATH='{model_path}' PYTHONUNBUFFERED=1 '{venv_python}' '{remote_stream_script}'"
            log.info("stream exec_command (remote): %s", cmd[:200])
            try:
                channel = client.get_transport().open_session()
                channel.exec_command(cmd)
            except Exception as e:
                err_exec = f"SSH 执行推流失败: {e}"
                _last_stream_error[demo_id] = err_exec
                raise HTTPException(500, err_exec)
            log.info("stream channel opened, waiting first frame (timeout=60s)")
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
        exit_status = None
        if target and demo_id in _stream_channels:
            ch = _stream_channels.get(demo_id)
            if ch:
                try:
                    parts = []
                    for _ in range(100):
                        if not ch.recv_stderr_ready():
                            break
                        data = ch.recv_stderr(8192)
                        if not data:
                            break
                        parts.append(data.decode("utf-8", errors="replace"))
                    stderr_text = "".join(parts).strip()
                    if not ch.exit_status_ready():
                        ch.close()
                    else:
                        exit_status = ch.recv_exit_status()
                        ch.close()
                except Exception as e:
                    log.warning("stream reading stderr/exit_status: %s", e)
            _stream_channels.pop(demo_id, None)
        if not target and demo_id in _stream_processes:
            proc = _stream_processes[demo_id]
            exit_status = proc.poll()
            try:
                stderr_text = proc.stderr.read().decode("utf-8", errors="replace").strip() if proc.stderr else ""
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if exit_status is None:
                exit_status = getattr(proc, "returncode", None)
            _stream_processes.pop(demo_id, None)
        err_msg = "stream_yolo 进程异常退出（未产出首帧）。请查看 Jetson 上摄像头与模型是否正常。"
        if exit_status is not None:
            err_msg = err_msg + f"\n\n进程退出码: {exit_status}"
        if stderr_text:
            err_msg = err_msg + "\n\n进程 stderr:\n" + stderr_text[:1500]
        _last_stream_error[demo_id] = err_msg
        log.warning("stream 503 exit_status=%s stderr_len=%d: %s", exit_status, len(stderr_text), err_msg[:200])
        raise HTTPException(503, err_msg)

    log.info("stream first frame ok demo_id=%s", demo_id)

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
