from __future__ import annotations

import shlex
import time

from .config import resolve_path
from .session_manager import DeployJob, Session, SessionManager
from .utils import LineBuffer


def run_deploy(manager: SessionManager, job: DeployJob, session: Session, demo: dict) -> None:
    deploy_cfg = demo.get("deploy", {})
    local_script = resolve_path(deploy_cfg.get("local_script_path", ""))
    remote_dir = deploy_cfg.get("remote_dir", "")
    remote_script_name = deploy_cfg.get("remote_script_name", "setup.sh")
    run_as_sudo = bool(deploy_cfg.get("run_as_sudo", False))

    if not local_script.exists():
        manager.set_job_status(job, "FAILED", exit_code=-1)
        manager.append_job_log(job, f"local script not found: {local_script}")
        return
    if not remote_dir:
        manager.set_job_status(job, "FAILED", exit_code=-1)
        manager.append_job_log(job, "remote_dir not configured")
        return

    ssh = session.ssh
    remote_script = f"{remote_dir.rstrip('/')}/{remote_script_name}"

    try:
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
            ssh.sftp_put(str(local_script), remote_script)
        except Exception as exc:
            manager.append_job_log(job, f"sftp upload failed, fallback to sudo tee: {exc}")
            ssh.put_file_with_sudo(str(local_script), remote_script)
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
            session.deployed_demos.add(demo.get("id", ""))
            manager.set_job_status(job, "DONE", exit_code=exit_code)
        else:
            manager.set_job_status(job, "FAILED", exit_code=exit_code)
    except Exception as exc:
        manager.append_job_log(job, f"deploy error: {exc}")
        manager.set_job_status(job, "FAILED", exit_code=-1)
    finally:
        pass
