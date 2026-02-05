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


def _build_run_cmd(remote_dir: str, python_bin: str | None, script_path: str | None, run_cfg: dict, port: int | None = None) -> str:
    run_type = run_cfg.get("type", "").lower()
    if run_type == "webui":
        if port is None:
            port = int(run_cfg.get("remote_port", 8090))
        cmd_tpl = run_cfg.get("run_cmd") or "live-vlm-webui --host 0.0.0.0 --port {port}"
        webui_bin = run_cfg.get("webui_bin") or "live-vlm-webui"
        webui_bin_q = shlex.quote(webui_bin)
        if "{bin}" in cmd_tpl:
            return cmd_tpl.format(port=port, bin=webui_bin_q)
        if cmd_tpl.split():
            first = cmd_tpl.split()[0]
            if "live-vlm-webui" == first and webui_bin:
                cmd_tpl = cmd_tpl.replace(first, webui_bin_q, 1)
        return cmd_tpl.format(port=port)
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
        shlex.quote(python_bin or "python3"),
        shlex.quote(script_path or ""),
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


def _bash_lc(cmd: str) -> str:
    safe = cmd.replace("\\", "\\\\").replace('"', '\\"')
    return f'bash -lc "{safe}"'


def run_demo(manager: SessionManager, run: RunSession, session: Session, demo: dict) -> None:
    run_cfg = demo.get("run", {})
    deploy_cfg = demo.get("deploy", {})
    remote_dir = deploy_cfg.get("remote_dir", "")
    if not remote_dir:
        manager.set_run_status(run, "ERROR")
        manager.append_run_log(run, "remote_dir not configured")
        return

    run_type = run_cfg.get("type", "").lower()
    local_py = None
    python_bin = None
    remote_py = None
    health_timeout = float(run_cfg.get("health_timeout", RUN_HEALTH_TIMEOUT))
    health_interval = float(run_cfg.get("health_interval", RUN_HEALTH_INTERVAL))
    health_path = run_cfg.get("health_path", "/health")
    health_any = bool(run_cfg.get("health_any", False))
    health_scheme = str(run_cfg.get("scheme", "http")).lower()
    verify_ssl = bool(run_cfg.get("verify_ssl", True))
    if run_type == "webui" and run_cfg.get("allow_insecure", False):
        verify_ssl = False

    ssh = session.ssh

    try:
        ssh.mkdir_p(remote_dir, sudo=False)
    except Exception:
        try:
            ssh.mkdir_p(remote_dir, sudo=True)
            try:
                ssh.run_command(
                    f"chown -R {shlex.quote(session.username)}:{shlex.quote(session.username)} {shlex.quote(remote_dir)}",
                    sudo=True,
                )
            except Exception:
                pass
        except Exception as exc:
            manager.set_run_status(run, "ERROR")
            manager.append_run_log(run, f"remote_dir create failed: {exc}")
            return

    if run_type != "webui":
        local_py = resolve_path(run_cfg.get("remote_py_script_path", ""))
        if not local_py.exists():
            manager.set_run_status(run, "ERROR")
            manager.append_run_log(run, f"missing local payload: {local_py}")
            return

        venv_rel = run_cfg.get("venv_rel_path", "")
        python_bin = _remote_path(remote_dir, venv_rel)
        remote_py = _remote_path(remote_dir, Path(local_py).name)

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
    else:
        home_cmd = _bash_lc("echo $HOME")
        exit_code, stdout, _ = ssh.run_command(home_cmd)
        home = (stdout or "").strip().splitlines()
        home_dir = home[-1] if exit_code == 0 and home else ""
        candidates = []
        if home_dir:
            candidates.append(f"{home_dir}/.local/bin/live-vlm-webui")
        candidates.extend(["/usr/local/bin/live-vlm-webui", "/usr/bin/live-vlm-webui"])
        webui_bin = None
        for cand in candidates:
            check_cmd = _bash_lc(f"test -x {shlex.quote(cand)} && echo {shlex.quote(cand)}")
            exit_code, stdout, _ = ssh.run_command(check_cmd)
            out = (stdout or "").strip()
            if exit_code == 0 and out:
                webui_bin = out.splitlines()[-1]
                break
        if not webui_bin:
            exit_code, stdout, _ = ssh.run_command(_bash_lc("command -v live-vlm-webui || true"))
            out = (stdout or "").strip()
            if out:
                webui_bin = out.splitlines()[-1]
        if not webui_bin:
            manager.set_run_status(run, "ERROR")
            manager.append_run_log(run, "live-vlm-webui not found on Jetson. Please deploy first.")
            return
        run_cfg = dict(run_cfg)
        run_cfg["webui_bin"] = webui_bin

    desired_port = int(run_cfg.get("remote_port", 8090))
    port = _prepare_remote_port(manager, run, ssh, desired_port)
    if port is None:
        manager.set_run_status(run, "ERROR")
        return

    run.remote_port = port
    run.remote_script = remote_py
    if run_type == "webui":
        run.remote_script = run_cfg.get("webui_bin") or "live-vlm-webui"

    cmd = _build_run_cmd(remote_dir, python_bin, remote_py, run_cfg, port=port)
    shell_cmd = f"cd {shlex.quote(remote_dir)}; nohup {cmd} > run.log 2>&1 & echo $!"
    full_cmd = _bash_lc(shell_cmd)

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
        run.remote_pid = None
        manager.append_run_log(run, f"warn: invalid pid: {pid_str}")

    if run_type == "webui":
        startup_timeout = float(run_cfg.get("startup_timeout", health_timeout))
        manager.append_run_log(run, "waiting for WebUI to listen...")
        detected_port = _wait_listen_port(
            ssh,
            preferred_port=port,
            pid=run.remote_pid,
            process_name="live-vlm-webui",
            timeout=startup_timeout,
            interval=health_interval,
        )
        if detected_port is None:
            manager.append_run_log(run, "webui did not listen on any port")
            _append_remote_log_tail(manager, run, ssh, remote_dir, lines=160)
            _stop_remote(ssh, run.remote_pid)
            manager.set_run_status(run, "ERROR", info=_run_info(run, session.host, health_scheme))
            return
        if detected_port != port:
            manager.append_run_log(run, f"webui port detected: {detected_port}")
            port = detected_port
            run.remote_port = detected_port

    try:
        tunnel = _start_tunnel_with_retry(
            manager,
            run,
            ssh,
            session,
            port,
            attempts=5 if run_type == "webui" else 2,
            interval=health_interval,
        )
    except Exception as exc:
        manager.append_run_log(run, f"tunnel start failed: {exc}")
        if run_type == "webui":
            manager.append_run_log(run, "tunnel unavailable, trying direct WebUI access...")
            ok, detail = _wait_health(
                session.host,
                port,
                scheme=health_scheme,
                timeout=health_timeout,
                interval=health_interval,
                path=health_path,
                accept_any=health_any,
                verify_ssl=verify_ssl,
            )
            if ok:
                run.local_port = None
                manager.set_run_status(run, "RUNNING", info=_run_info(run, session.host, health_scheme))
                manager.append_run_log(run, "webui is running (direct access)")
                return
            if detail:
                manager.append_run_log(run, f"direct health check failed: {detail}")
        _stop_remote(ssh, run.remote_pid)
        manager.set_run_status(run, "ERROR")
        return

    run.tunnel = tunnel
    if run.local_port is None:
        manager.append_run_log(run, "tunnel did not return local port")
        _stop_remote(ssh, run.remote_pid)
        manager.set_run_status(run, "ERROR")
        return
    manager.append_run_log(run, f"tunnel ready: http://127.0.0.1:{run.local_port}")
    manager.set_run_status(run, "STARTING", info=_run_info(run, session.host, health_scheme))

    ok, detail = _wait_health(
        "127.0.0.1",
        run.local_port,
        scheme=health_scheme,
        timeout=health_timeout,
        interval=health_interval,
        path=health_path,
        accept_any=health_any,
        verify_ssl=verify_ssl,
    )
    if not ok:
        if detail:
            manager.append_run_log(run, f"health check failed: {detail}")
        else:
            manager.append_run_log(run, "health check timeout")
        _append_remote_log_tail(manager, run, ssh, remote_dir, lines=160)
        _stop_remote(ssh, run.remote_pid)
        tunnel.stop()
        manager.set_run_status(run, "ERROR", info=_run_info(run, session.host, health_scheme))
        return

    if run.remote_pid is None and run.remote_port is not None:
        try:
            pids = _find_listening_pids(ssh, run.remote_port) or []
            if pids:
                run.remote_pid = pids[0]
                manager.append_run_log(run, f"remote PID detected: {run.remote_pid}")
        except Exception:
            pass

    manager.set_run_status(run, "RUNNING", info=_run_info(run, session.host, health_scheme))
    manager.append_run_log(run, "stream server is running")


def _wait_health(
    host: str,
    port: int,
    scheme: str = "http",
    timeout: float = RUN_HEALTH_TIMEOUT,
    interval: float = RUN_HEALTH_INTERVAL,
    path: str = "/health",
    accept_any: bool = False,
    verify_ssl: bool = True,
) -> tuple[bool, str | None]:
    path = path if path.startswith("/") else f"/{path}"
    url = f"{scheme}://{host}:{port}{path}"
    deadline = time.time() + timeout
    last_detail: str | None = None
    # Avoid using system proxies to prevent socks scheme errors in httpx.
    with httpx.Client(timeout=2.0, trust_env=False, verify=verify_ssl) as client:
        while time.time() < deadline:
            try:
                r = client.get(url)
                if accept_any:
                    return True, None
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


def _run_info(run: RunSession, host: str | None = None, scheme: str = "http") -> dict:
    info: dict = {}
    if run.remote_port is not None:
        info["remote_port"] = run.remote_port
        if host:
            info["remote_host"] = host
            info["remote_url"] = f"{scheme}://{host}:{run.remote_port}"
            info["scheme"] = scheme
    if run.local_port is not None:
        info["local_port"] = run.local_port
        info["local_url"] = f"{scheme}://127.0.0.1:{run.local_port}"
    return info


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


def _extract_listen_port(token: str) -> int | None:
    if not token:
        return None
    local = token.strip()
    if local.startswith("[") and local.endswith("]"):
        local = local[1:-1]
    if ":" not in local:
        return None
    port_str = local.rsplit(":", 1)[-1]
    if port_str.isdigit():
        return int(port_str)
    return None


def _find_pids_by_name(ssh, pattern: str) -> list[int]:
    cmd = f"pgrep -f {shlex.quote(pattern)}"
    exit_code, stdout, _ = ssh.run_command(cmd)
    if exit_code != 0:
        return []
    pids: list[int] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _find_listening_ports(ssh, pids: list[int] | None = None, process_name: str | None = None) -> list[int]:
    exit_code, stdout, _ = ssh.run_command("ss -ltnp")
    if exit_code != 0:
        return []
    ports: list[int] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("state"):
            continue
        if pids:
            if not any(f"pid={pid}" in line for pid in pids):
                continue
        if process_name and process_name not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        port = _extract_listen_port(parts[3])
        if port is not None:
            ports.append(port)
    return sorted(set(ports))


def _is_port_listening(ssh, port: int) -> bool:
    exit_code, stdout, _ = ssh.run_command(f"ss -ltn | grep ':{port} '")
    if exit_code == 0 and (stdout or "").strip():
        return True
    exit_code, stdout, _ = ssh.run_command(f"ss -ltn | grep ':{port}$'")
    return exit_code == 0 and (stdout or "").strip() != ""


def _resolve_remote_host(ssh, port: int) -> str:
    exit_code, stdout, _ = ssh.run_command(f"ss -ltn | grep ':{port} '")
    if exit_code != 0:
        return "127.0.0.1"
    for line in (stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3]
        if addr.startswith("[::]") or addr.startswith("::"):
            return "::1"
    return "127.0.0.1"


def _wait_listen_port(
    ssh,
    preferred_port: int,
    pid: int | None,
    process_name: str | None,
    timeout: float,
    interval: float,
) -> int | None:
    deadline = time.time() + timeout
    pids = [pid] if pid else []
    while time.time() < deadline:
        if _is_port_listening(ssh, preferred_port):
            return preferred_port
        if not pids and process_name:
            pids = _find_pids_by_name(ssh, process_name)
        name_filter = None if pids else process_name
        ports = _find_listening_ports(ssh, pids=pids if pids else None, process_name=name_filter)
        if preferred_port in ports:
            return preferred_port
        if ports:
            return ports[0]
        time.sleep(interval)
    return None


def _start_tunnel_with_retry(
    manager: SessionManager,
    run: RunSession,
    ssh,
    session: Session,
    remote_port: int,
    attempts: int,
    interval: float,
) -> Tunnel:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        if not _is_port_listening(ssh, remote_port):
            time.sleep(interval)
            continue
        remote_host = _resolve_remote_host(ssh, remote_port)
        manager.append_run_log(
            run, f"tunnel attempt {attempt}/{attempts} -> {remote_host}:{remote_port}"
        )
        local_port = get_free_port()
        tunnel_cfg = TunnelConfig(
            ssh=SSHConfig(session.host, session.port, session.username, session.password),
            remote_host=remote_host,
            remote_port=remote_port,
            local_port=local_port,
        )
        tunnel = Tunnel(tunnel_cfg)
        try:
            tunnel.start()
            run.local_port = local_port
            return tunnel
        except Exception as exc:
            last_exc = exc
            manager.append_run_log(run, f"tunnel start failed (attempt {attempt}/{attempts}): {exc}")
            try:
                tunnel.stop()
            except Exception:
                pass
            time.sleep(interval)
    if last_exc:
        raise last_exc
    raise RuntimeError("tunnel start failed: remote port not listening")


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
