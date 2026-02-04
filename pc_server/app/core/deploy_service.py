from __future__ import annotations

import shlex
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .config import resolve_path
from .session_manager import DeployJob, Session, SessionManager
from .utils import LineBuffer


def _build_raw_github_url(repo_url: str, ref: str, path: str) -> str | None:
    if not repo_url:
        return None
    parsed = urlparse(repo_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc not in {"github.com", "www.github.com"}:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    clean_path = path.lstrip("/")
    if not clean_path:
        return None
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{clean_path}"


def _load_script_bytes(deploy_cfg: dict) -> tuple[bytes, str]:
    script_url = deploy_cfg.get("local_script_url", "")
    if not script_url:
        repo_url = deploy_cfg.get("script_repo", "")
        script_ref = deploy_cfg.get("script_ref", "main")
        script_path = deploy_cfg.get("script_path", "")
        script_url = _build_raw_github_url(repo_url, script_ref, script_path) or ""

    if script_url:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(script_url)
        if resp.status_code != 200:
            raise RuntimeError(f"download failed ({resp.status_code}): {script_url}")
        if resp.content is None or len(resp.content) == 0:
            raise RuntimeError(f"download empty: {script_url}")
        return resp.content, script_url

    local_script_path = deploy_cfg.get("local_script_path", "")
    if local_script_path:
        local_script = resolve_path(local_script_path)
        if local_script.exists():
            return local_script.read_bytes(), str(local_script)

    raise FileNotFoundError("deploy script not configured")


def run_deploy(manager: SessionManager, job: DeployJob, session: Session, demo: dict) -> None:
    deploy_cfg = demo.get("deploy", {})
    remote_dir = deploy_cfg.get("remote_dir", "")
    remote_script_name = deploy_cfg.get("remote_script_name", "setup.sh")
    run_as_sudo = bool(deploy_cfg.get("run_as_sudo", False))
    marker_path = deploy_cfg.get("marker_path", "")
    version = deploy_cfg.get("version") or demo.get("status", {}).get("version", "")

    if not remote_dir:
        manager.set_job_status(job, "FAILED", exit_code=-1)
        manager.append_job_log(job, "remote_dir not configured")
        return

    ssh = session.ssh
    remote_script = f"{remote_dir.rstrip('/')}/{remote_script_name}"

    try:
        try:
            script_data, script_source = _load_script_bytes(deploy_cfg)
            manager.append_job_log(job, f"using deploy script: {script_source}")
        except Exception as exc:
            manager.set_job_status(job, "FAILED", exit_code=-1)
            manager.append_job_log(job, f"load deploy script failed: {exc}")
            return
        normalized = script_data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if normalized != script_data:
            script_data = normalized
            manager.append_job_log(job, "note: normalized CRLF to LF before upload")

        manager.set_job_status(job, "UPLOADING")
        try:
            ssh.mkdir_p(remote_dir, sudo=False)
        except Exception:
            ssh.mkdir_p(remote_dir, sudo=True)
            try:
                ssh.run_command(
                    f"chown -R {shlex.quote(session.username)}:{shlex.quote(session.username)} {shlex.quote(remote_dir)}",
                    sudo=True,
                )
            except Exception:
                pass
        used_sudo_upload = False
        try:
            ssh.sftp_put_bytes(script_data, remote_script)
        except Exception as exc:
            manager.append_job_log(job, f"sftp upload failed, fallback to sudo tee: {exc}")
            ssh.write_file_sudo(remote_script, script_data)
            used_sudo_upload = True
        ssh.run_command(f"chmod +x {shlex.quote(remote_script)}", sudo=run_as_sudo or used_sudo_upload)
    except Exception as exc:
        manager.set_job_status(job, "FAILED", exit_code=-1)
        manager.append_job_log(job, f"upload failed: {exc}")
        return

    command = (
        "bash -lc "
        + shlex.quote(
            "set -euo pipefail; "
            f"cd {shlex.quote(remote_dir)}; "
            "if command -v stdbuf >/dev/null 2>&1; then "
            f"stdbuf -oL -eL bash ./{shlex.quote(remote_script_name)}; "
            "else "
            f"bash ./{shlex.quote(remote_script_name)}; "
            "fi"
        )
    )

    manager.set_job_status(job, "RUNNING")

    try:
        channel = ssh.exec_command(command, sudo=run_as_sudo)
        job.channel = channel
        out_buf = LineBuffer()
        err_buf = LineBuffer()
        while True:
            if job.cancel_event.is_set():
                manager.append_job_log(job, "deploy cancelled by user")
                manager.set_job_status(job, "CANCELLED", exit_code=-2)
                try:
                    channel.close()
                except Exception:
                    pass
                return

            if channel.recv_ready():
                data = channel.recv(4096).decode("utf-8", errors="replace")
                for line in out_buf.feed(data):
                    manager.append_job_log(job, line)
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                for line in err_buf.feed(data):
                    manager.append_job_log(job, line)

            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            time.sleep(0.05)

        for line in out_buf.flush():
            manager.append_job_log(job, line)
        for line in err_buf.flush():
            manager.append_job_log(job, line)

        exit_code = channel.recv_exit_status()
        if exit_code == 0:
            if run_as_sudo:
                try:
                    ssh.run_command(
                        f"chown -R {shlex.quote(session.username)}:{shlex.quote(session.username)} {shlex.quote(remote_dir)}",
                        sudo=True,
                    )
                except Exception:
                    manager.append_job_log(job, "warn: chown remote_dir failed; run may need sudo")

            if marker_path:
                marker_full = (
                    marker_path
                    if marker_path.startswith("/")
                    else f"{remote_dir.rstrip('/')}/{marker_path.lstrip('/')}"
                )
                installed_at = datetime.now(timezone.utc).isoformat()
                marker_payload = f"installed_at={installed_at}\nversion={version}\n"
                marker_bytes = marker_payload.encode("utf-8")
                wrote_marker = False
                try:
                    ssh.sftp_put_bytes(marker_bytes, marker_full)
                    wrote_marker = True
                except Exception as exc:
                    manager.append_job_log(job, f"warn: sftp write marker failed, fallback to sudo: {exc}")
                    try:
                        ssh.write_file_sudo(marker_full, marker_bytes)
                        wrote_marker = True
                    except Exception as exc2:
                        manager.append_job_log(job, f"warn: write marker failed: {exc2}")
                if wrote_marker and run_as_sudo:
                    try:
                        ssh.run_command(
                            f"chown {shlex.quote(session.username)}:{shlex.quote(session.username)} {shlex.quote(marker_full)}",
                            sudo=True,
                        )
                    except Exception:
                        manager.append_job_log(job, "warn: chown marker failed")

            session.deployed_demos.add(demo.get("id", ""))
            manager.set_job_status(job, "DONE", exit_code=exit_code)
        else:
            manager.set_job_status(job, "FAILED", exit_code=exit_code)
    except Exception as exc:
        manager.append_job_log(job, f"deploy error: {exc}")
        manager.set_job_status(job, "FAILED", exit_code=-1)
    finally:
        pass
