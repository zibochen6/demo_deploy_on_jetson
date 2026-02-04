from __future__ import annotations

import shlex
import time
from pathlib import Path

import httpx

from .config import RUN_HEALTH_INTERVAL, RUN_HEALTH_TIMEOUT, resolve_path
from .session_manager import RunSession, Session, SessionManager
from .ssh_client import SSHConfig
from .tunnel import Tunnel, TunnelConfig
from .utils import get_free_port


def _remote_path(remote_dir: str, relative_or_abs: str) -> str:
    p = Path(relative_or_abs)
    if p.is_absolute():
        return str(p)
    return str(Path(remote_dir) / p)


def _build_run_cmd(remote_dir: str, python_bin: str, script_path: str, run_cfg: dict) -> str:
    camera = run_cfg.get("default_camera", "usb")
    usb_index = int(run_cfg.get("usb_index", 0))
    csi = run_cfg.get("csi_params", {}) or {}
    width = int(csi.get("width", 1280))
    height = int(csi.get("height", 720))
    flip = int(csi.get("flip", 0))
    model_hint = run_cfg.get("model_hint", "yolo11n.pt")
    model_path = _remote_path(remote_dir, f"yolo11/{model_hint}")
    port = int(run_cfg.get("remote_port", 8090))

    args = [
        shlex.quote(python_bin),
        shlex.quote(script_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--camera",
        shlex.quote(camera),
        "--usb-index",
        str(usb_index),
        "--width",
        str(width),
        "--height",
        str(height),
        "--flip",
        str(flip),
        "--model",
        shlex.quote(model_path),
    ]
    return " ".join(args)


def run_demo(manager: SessionManager, run: RunSession, session: Session, demo: dict) -> None:
    run_cfg = demo.get("run", {})
    deploy_cfg = demo.get("deploy", {})
    remote_dir = deploy_cfg.get("remote_dir", "")
    if not remote_dir:
        manager.set_run_status(run, "ERROR")
        manager.append_run_log(run, "remote_dir not configured")
        return

    local_py = resolve_path(run_cfg.get("remote_py_script_path", ""))
    if not local_py.exists():
        manager.set_run_status(run, "ERROR")
        manager.append_run_log(run, f"missing local payload: {local_py}")
        return

    venv_rel = run_cfg.get("venv_rel_path", "")
    python_bin = _remote_path(remote_dir, venv_rel)
    remote_py = _remote_path(remote_dir, Path(local_py).name)

    ssh = session.ssh

    try:
        exit_code, _, _ = ssh.run_command(f"test -w {shlex.quote(remote_dir)}")
        if exit_code != 0:
            ssh.run_command(
                f"chown -R {shlex.quote(session.username)}:{shlex.quote(session.username)} {shlex.quote(remote_dir)}",
                sudo=True,
            )
    except Exception as exc:
        manager.append_run_log(run, f"warn: remote_dir permission check failed: {exc}")

    try:
        manager.append_run_log(run, f"uploading payload -> {remote_py}")
        try:
            ssh.sftp_put(str(local_py), remote_py)
        except Exception as exc:
            manager.append_run_log(run, f"sftp upload failed, fallback to sudo tee: {exc}")
            ssh.put_file_with_sudo(str(local_py), remote_py)
            try:
                ssh.run_command(
                    f"chown {shlex.quote(session.username)}:{shlex.quote(session.username)} {shlex.quote(remote_py)}",
                    sudo=True,
                )
            except Exception:
                pass
    except Exception as exc:
        manager.set_run_status(run, "ERROR")
        manager.append_run_log(run, f"upload payload failed: {exc}")
        return

    cmd = _build_run_cmd(remote_dir, python_bin, remote_py, run_cfg)
    full_cmd = f"cd '{remote_dir}' && nohup {cmd} > run.log 2>&1 & echo $!"

    try:
        manager.append_run_log(run, "starting remote service...")
        exit_code, stdout, stderr = ssh.run_command(full_cmd)
    except Exception as exc:
        manager.set_run_status(run, "ERROR")
        manager.append_run_log(run, f"start command failed: {exc}")
        return

    if exit_code != 0:
        manager.set_run_status(run, "ERROR")
        manager.append_run_log(run, f"start command error: {stderr.strip()}")
        return

    pid_str = (stdout.strip().splitlines() or [""])[-1]
    try:
        run.remote_pid = int(pid_str)
        manager.append_run_log(run, f"remote PID: {run.remote_pid}")
    except ValueError:
        manager.set_run_status(run, "ERROR")
        manager.append_run_log(run, f"invalid pid: {pid_str}")
        return

    local_port = get_free_port()
    tunnel_cfg = TunnelConfig(
        ssh=SSHConfig(session.host, session.port, session.username, session.password),
        remote_host="127.0.0.1",
        remote_port=int(run_cfg.get("remote_port", 8090)),
        local_port=local_port,
    )
    tunnel = Tunnel(tunnel_cfg)
    try:
        tunnel.start()
    except Exception as exc:
        manager.append_run_log(run, f"tunnel start failed: {exc}")
        _stop_remote(ssh, run.remote_pid)
        manager.set_run_status(run, "ERROR")
        return

    run.tunnel = tunnel
    run.local_port = local_port
    manager.append_run_log(run, f"tunnel ready: http://127.0.0.1:{local_port}")
    manager.set_run_status(run, "STARTING")

    if not _wait_health(local_port):
        manager.append_run_log(run, "health check timeout")
        _stop_remote(ssh, run.remote_pid)
        tunnel.stop()
        manager.set_run_status(run, "ERROR")
        return

    manager.set_run_status(run, "RUNNING")
    manager.append_run_log(run, "stream server is running")


def _wait_health(local_port: int) -> bool:
    url = f"http://127.0.0.1:{local_port}/health"
    deadline = time.time() + RUN_HEALTH_TIMEOUT
    # 本地健康检查不走系统代理，避免 socks 等 scheme 导致 httpx 报错
    with httpx.Client(timeout=2.0, trust_env=False) as client:
        while time.time() < deadline:
            try:
                r = client.get(url)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(RUN_HEALTH_INTERVAL)
    return False


def _stop_remote(ssh, pid: int | None) -> None:
    if not pid:
        return
    try:
        ssh.run_command(f"kill {pid}")
        time.sleep(0.5)
        ssh.run_command(f"kill -9 {pid}")
    except Exception:
        pass


def stop_run(manager: SessionManager, run: RunSession, session: Session) -> None:
    if run.tunnel is not None:
        try:
            run.tunnel.stop()
        except Exception:
            pass
        run.tunnel = None
    _stop_remote(session.ssh, run.remote_pid)
    run.remote_pid = None
    run.local_port = None
    manager.set_run_status(run, "STOPPED")
    manager.remove_run_session(run.run_id)
