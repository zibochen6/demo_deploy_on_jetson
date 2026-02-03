from __future__ import annotations

from dataclasses import dataclass
import shlex
import time
from typing import Optional

import paramiko

from .config import SSH_BANNER_TIMEOUT, SSH_CONNECT_TIMEOUT


@dataclass
class SSHConfig:
    host: str
    port: int
    username: str
    password: str
    sudo_password: str | None = None


class SSHClientWrapper:
    def __init__(self, config: SSHConfig):
        self.config = config
        self.client: Optional[paramiko.SSHClient] = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.config.host,
            port=self.config.port,
            username=self.config.username,
            password=self.config.password,
            timeout=SSH_CONNECT_TIMEOUT,
            banner_timeout=SSH_BANNER_TIMEOUT,
        )
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            finally:
                self.client = None

    def _ensure_client(self) -> paramiko.SSHClient:
        if self.client is None:
            raise RuntimeError("SSH client not connected")
        return self.client

    def exec_command(self, command: str, sudo: bool = False, timeout: int | None = None) -> paramiko.Channel:
        client = self._ensure_client()
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport not available")
        channel = transport.open_session()
        if sudo:
            channel.get_pty()
            command = f"sudo -S -p '' {command}"
        channel.exec_command(command)
        if sudo:
            sudo_pw = self.config.sudo_password if self.config.sudo_password is not None else self.config.password
            channel.send((sudo_pw + "\n").encode("utf-8"))
        if timeout:
            channel.settimeout(timeout)
        return channel

    def run_command(self, command: str, sudo: bool = False, timeout: int | None = None) -> tuple[int, str, str]:
        channel = self.exec_command(command, sudo=sudo, timeout=timeout)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        while True:
            if channel.recv_ready():
                stdout_chunks.append(channel.recv(4096))
            if channel.recv_stderr_ready():
                stderr_chunks.append(channel.recv_stderr(4096))
            if channel.exit_status_ready():
                break
        while channel.recv_ready():
            stdout_chunks.append(channel.recv(4096))
        while channel.recv_stderr_ready():
            stderr_chunks.append(channel.recv_stderr(4096))
        exit_code = channel.recv_exit_status()
        channel.close()
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return exit_code, stdout, stderr

    def sftp_put(self, local_path: str, remote_path: str) -> None:
        client = self._ensure_client()
        sftp = client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()

    def mkdir_p(self, remote_dir: str, sudo: bool = False) -> None:
        cmd = f"mkdir -p '{remote_dir}'"
        exit_code, _, stderr = self.run_command(cmd, sudo=sudo)
        if exit_code != 0:
            raise RuntimeError(f"mkdir failed: {stderr.strip()}")

    def write_file_sudo(self, remote_path: str, data: bytes) -> None:
        client = self._ensure_client()
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport not available")
        channel = transport.open_session()
        channel.get_pty()
        cmd = f"sudo -S -p '' tee {shlex.quote(remote_path)} > /dev/null"
        channel.exec_command(cmd)
        sudo_pw = self.config.sudo_password if self.config.sudo_password is not None else self.config.password
        channel.send((sudo_pw + "\n").encode("utf-8"))
        channel.send(data)
        channel.shutdown_write()
        while not channel.exit_status_ready():
            if channel.recv_ready():
                channel.recv(4096)
            if channel.recv_stderr_ready():
                channel.recv_stderr(4096)
            time.sleep(0.05)
        while channel.recv_ready():
            channel.recv(4096)
        while channel.recv_stderr_ready():
            channel.recv_stderr(4096)
        exit_code = channel.recv_exit_status()
        channel.close()
        if exit_code != 0:
            raise RuntimeError("sudo tee failed")

    def put_file_with_sudo(self, local_path: str, remote_path: str) -> None:
        with open(local_path, "rb") as f:
            data = f.read()
        self.write_file_sudo(remote_path, data)
