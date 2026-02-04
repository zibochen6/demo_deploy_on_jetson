from __future__ import annotations

import shlex
import time
from pathlib import Path, PurePosixPath
import re

import httpx

from .config import RUN_HEALTH_INTERVAL, RUN_HEALTH_TIMEOUT, resolve_path
from .session_manager import RunSession, Session, SessionManager
from .ssh_client import SSHConfig
from .tunnel import Tunnel, TunnelConfig
from .utils import get_free_port


def _remote_path(remote_dir: str, relative_or_abs: str) -> str:
    p = PurePosixPath(relative_or_abs)
    if p.is_absolute():
        return str(p)
    return str(PurePosixPath(remote_dir) / p)


def _build_run_cmd(remote_dir: str, python_bin: str, script_path: str, run_cfg: dict, port: int | None = None) -> str:
    camera = run_cfg.get("default_camera", "usb")
    usb_index = int(run_cfg.get("usb_index", 0))
    csi = run_cfg.get("csi_params", {}) or {}
    width = int(csi.get("width", 1280))
    height = int(csi.get("height", 720))
    flip = int(csi.get("flip", 0))
    model_hint = run_cfg.get("model_hint", "yolo11n.pt")
    model_path = _remote_path(remote_dir, f"yolo11/{model_hint}")
    if port is None:
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

    desired_port = int(run_cfg.get("remote_port", 8090))
    port = _prepare_remote_port(manager, run, ssh, desired_port)
    if port is None:
        manager.set_run_status(run, "ERROR")
        return

    run.remote_port = port
    run.remote_script = remote_py

    cmd = _build_run_cmd(remote_dir, python_bin, remote_py, run_cfg, port=port)
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
        remote_port=port,
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

    health_timeout = float(run_cfg.get("health_timeout", RUN_HEALTH_TIMEOUT))
    health_interval = float(run_cfg.get("health_interval", RUN_HEALTH_INTERVAL))
    ok, detail = _wait_health(local_port, timeout=health_timeout, interval=health_interval)
    if not ok:
        if detail:
            manager.append_run_log(run, f"health check failed: {detail}")
        else:
            manager.append_run_log(run, "health check timeout")
        _append_remote_log_tail(manager, run, ssh, remote_dir, lines=160)
        _stop_remote(ssh, run.remote_pid)
        tunnel.stop()
        manager.set_run_status(run, "ERROR")
        return

    manager.set_run_status(run, "RUNNING")
    manager.append_run_log(run, "stream server is running")


def _wait_health(
    local_port: int,
    timeout: float = RUN_HEALTH_TIMEOUT,
    interval: float = RUN_HEALTH_INTERVAL,
) -> tuple[bool, str | None]:
    url = f"http://127.0.0.1:{local_port}/health"
    deadline = time.time() + timeout
    last_detail: str | None = None
    # Avoid using system proxies to prevent socks scheme errors in httpx.
    with httpx.Client(timeout=2.0, trust_env=False) as client:
        while time.time() < deadline:
            try:
                r = client.get(url)
                if r.status_code == 200:
                    return True, None
                if r.headers.get("content-type", "").startswith("application/json"):
                    try:
                        payload = r.json()
                        detail = payload.get("detail")
                        if isinstance(detail, str):
                            last_detail = detail
                    except Exception:
                        pass
                if r.status_code >= 500 and last_detail:
                    return False, last_detail
            except Exception:
                pass
            time.sleep(interval)
    return False, last_detail


PID_RE = re.compile(r"pid=(\d+)")


def _find_listening_pids(ssh, port: int) -> list[int] | None:
    commands = [
        f"ss -ltnp 'sport = :{port}'",
        f"ss -ltnp | grep ':{port} '",
        f"lsof -t -iTCP:{port} -sTCP:LISTEN",
    ]
    for cmd in commands:
        exit_code, stdout, stderr = ssh.run_command(cmd)
        output = (stdout or "").strip()
        if exit_code == 0:
            if not output:
                return []
            pids = set(int(m.group(1)) for m in PID_RE.finditer(output))
            if not pids and cmd.startswith("lsof"):
                for line in output.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pids.add(int(line))
            return sorted(pids)
        if stderr and "not found" in stderr:
            continue
    return None


def _kill_pids(ssh, pids: list[int]) -> None:
    if not pids:
        return
    joined = " ".join(str(pid) for pid in pids)
    exit_code, _, _ = ssh.run_command(f"kill {joined}")
    if exit_code != 0:
        ssh.run_command(f"kill {joined}", sudo=True)
    time.sleep(0.4)
    exit_code, _, _ = ssh.run_command(f"kill -9 {joined}")
    if exit_code != 0:
        ssh.run_command(f"kill -9 {joined}", sudo=True)


def _kill_by_port(ssh, port: int) -> None:
    pids = _find_listening_pids(ssh, port)
    if pids:
        _kill_pids(ssh, pids)


def _kill_by_script(ssh, script_path: str) -> None:
    if not script_path:
        return
    cmd = f"pgrep -f {shlex.quote(script_path)}"
    exit_code, stdout, stderr = ssh.run_command(cmd)
    if exit_code != 0:
        return
    pids: list[int] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    if pids:
        _kill_pids(ssh, pids)


def _find_free_port(ssh, start_port: int, max_tries: int = 20) -> int | None:
    port = start_port
    for _ in range(max_tries):
        pids = _find_listening_pids(ssh, port)
        if pids is None:
            return None
        if not pids:
            return port
        port += 1
    return None


def _prepare_remote_port(manager: SessionManager, run: RunSession, ssh, desired_port: int) -> int | None:
    pids = _find_listening_pids(ssh, desired_port)
    if pids is None:
        return desired_port
    if not pids:
        return desired_port

    manager.append_run_log(run, f"port {desired_port} in use, trying to stop: {pids}")
    _kill_pids(ssh, pids)
    remaining = _find_listening_pids(ssh, desired_port)
    if remaining == []:
        manager.append_run_log(run, f"port {desired_port} released")
        return desired_port

    new_port = _find_free_port(ssh, desired_port + 1)
    if new_port is None:
        manager.append_run_log(run, f"port {desired_port} still in use; no free port found")
        return None
    manager.append_run_log(run, f"port {desired_port} still in use, switching to {new_port}")
    return new_port


def _cleanup_remote(ssh, pid: int | None, port: int | None, script_path: str | None) -> None:
    if pid:
        _stop_remote(ssh, pid)
    if port:
        _kill_by_port(ssh, port)
    if script_path:
        _kill_by_script(ssh, script_path)


def _append_remote_log_tail(
    manager: SessionManager,
    run: RunSession,
    ssh,
    remote_dir: str,
    lines: int = 120,
) -> None:
    if not remote_dir:
        return
    log_path = _remote_path(remote_dir, "run.log")
    try:
        exit_code, stdout, stderr = ssh.run_command(f"tail -n {lines} {shlex.quote(log_path)}")
    except Exception as exc:
        manager.append_run_log(run, f"read run.log failed: {exc}")
        return
    if exit_code != 0:
        msg = stderr.strip() or "unknown error"
        manager.append_run_log(run, f"read run.log failed: {msg}")
        return
    content = (stdout or "").strip()
    if not content:
        manager.append_run_log(run, "run.log is empty")
        return
    manager.append_run_log(run, "---- remote run.log (tail) ----")
    for line in content.splitlines():
        manager.append_run_log(run, line)


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
    _cleanup_remote(session.ssh, run.remote_pid, run.remote_port, run.remote_script)
    run.remote_pid = None
    run.remote_port = None
    run.remote_script = None
    run.local_port = None
    manager.set_run_status(run, "STOPPED")
    manager.remove_run_session(run.run_id)
