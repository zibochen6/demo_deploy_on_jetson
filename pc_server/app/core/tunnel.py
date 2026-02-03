from __future__ import annotations

from dataclasses import dataclass

from sshtunnel import SSHTunnelForwarder

from .ssh_client import SSHConfig


@dataclass
class TunnelConfig:
    ssh: SSHConfig
    remote_host: str
    remote_port: int
    local_port: int


class Tunnel:
    def __init__(self, config: TunnelConfig):
        self.config = config
        self.server: SSHTunnelForwarder | None = None

    def start(self) -> None:
        ssh = self.config.ssh
        self.server = SSHTunnelForwarder(
            (ssh.host, ssh.port),
            ssh_username=ssh.username,
            ssh_password=ssh.password,
            remote_bind_address=(self.config.remote_host, self.config.remote_port),
            local_bind_address=("127.0.0.1", self.config.local_port),
            allow_agent=False,
            host_pkey_directories=[],
        )
        self.server.start()

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.stop()
            finally:
                self.server = None
