from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from fastapi import WebSocket

from .config import LOG_BUFFER_LINES
from .ssh_client import SSHClientWrapper
from .tunnel import Tunnel
from .utils import RingBuffer, sanitize_log_line


@dataclass
class Session:
    session_id: str
    ssh: SSHClientWrapper
    host: str
    port: int
    username: str
    password: str
    deployed_demos: set[str] = field(default_factory=set)
    demo_overrides: Dict[str, dict] = field(default_factory=dict)


@dataclass
class DeployJob:
    job_id: str
    session_id: str
    demo_id: str
    status: str = "PENDING"
    exit_code: Optional[int] = None
    log_buffer: RingBuffer = field(default_factory=lambda: RingBuffer(LOG_BUFFER_LINES))
    ws_clients: Set[WebSocket] = field(default_factory=set)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    channel: object | None = None


@dataclass
class RunSession:
    run_id: str
    session_id: str
    demo_id: str
    status: str = "STARTING"
    remote_pid: Optional[int] = None
    remote_port: Optional[int] = None
    remote_script: Optional[str] = None
    tunnel: Optional[Tunnel] = None
    local_port: Optional[int] = None
    log_buffer: RingBuffer = field(default_factory=lambda: RingBuffer(LOG_BUFFER_LINES))
    ws_clients: Set[WebSocket] = field(default_factory=set)


class SessionManager:
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        self._loop = loop
        self._lock = threading.Lock()
        self.sessions: Dict[str, Session] = {}
        self.deploy_jobs: Dict[str, DeployJob] = {}
        self.run_sessions: Dict[str, RunSession] = {}
        self.deploy_by_demo: Dict[tuple[str, str], str] = {}
        self.run_by_demo: Dict[tuple[str, str], str] = {}

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def create_session(self, ssh: SSHClientWrapper) -> Session:
        session_id = str(uuid.uuid4())
        cfg = ssh.config
        session = Session(
            session_id=session_id,
            ssh=ssh,
            host=cfg.host,
            port=cfg.port,
            username=cfg.username,
            password=cfg.password,
        )
        with self._lock:
            self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self.sessions.get(session_id)

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            self.sessions.pop(session_id, None)

    def create_deploy_job(self, session_id: str, demo_id: str) -> DeployJob:
        job_id = str(uuid.uuid4())
        job = DeployJob(job_id=job_id, session_id=session_id, demo_id=demo_id)
        with self._lock:
            old_job_id = self.deploy_by_demo.get((session_id, demo_id))
            if old_job_id:
                self.deploy_jobs.pop(old_job_id, None)
            self.deploy_jobs[job_id] = job
            self.deploy_by_demo[(session_id, demo_id)] = job_id
        return job

    def get_deploy_job(self, job_id: str) -> Optional[DeployJob]:
        with self._lock:
            return self.deploy_jobs.get(job_id)

    def get_deploy_job_by_demo(self, session_id: str, demo_id: str) -> Optional[DeployJob]:
        with self._lock:
            job_id = self.deploy_by_demo.get((session_id, demo_id))
            return self.deploy_jobs.get(job_id) if job_id else None

    def remove_deploy_job(self, job_id: str) -> None:
        with self._lock:
            job = self.deploy_jobs.pop(job_id, None)
            if job:
                self.deploy_by_demo.pop((job.session_id, job.demo_id), None)

    def create_run_session(self, session_id: str, demo_id: str) -> RunSession:
        run_id = str(uuid.uuid4())
        run = RunSession(run_id=run_id, session_id=session_id, demo_id=demo_id)
        with self._lock:
            self.run_sessions[run_id] = run
            self.run_by_demo[(session_id, demo_id)] = run_id
        return run

    def get_run_session(self, run_id: str) -> Optional[RunSession]:
        with self._lock:
            return self.run_sessions.get(run_id)

    def get_run_by_demo(self, session_id: str, demo_id: str) -> Optional[RunSession]:
        with self._lock:
            run_id = self.run_by_demo.get((session_id, demo_id))
            return self.run_sessions.get(run_id) if run_id else None

    def remove_run_session(self, run_id: str) -> None:
        with self._lock:
            run = self.run_sessions.pop(run_id, None)
            if run:
                self.run_by_demo.pop((run.session_id, run.demo_id), None)

    def append_job_log(self, job: DeployJob, line: str) -> None:
        cleaned = sanitize_log_line(line)
        job.log_buffer.append(cleaned)
        self._broadcast(job.ws_clients, {"type": "log", "data": cleaned})

    def append_run_log(self, run: RunSession, line: str) -> None:
        cleaned = sanitize_log_line(line)
        run.log_buffer.append(cleaned)
        self._broadcast(run.ws_clients, {"type": "log", "data": cleaned})

    def set_job_status(self, job: DeployJob, status: str, exit_code: Optional[int] = None) -> None:
        job.status = status
        if exit_code is not None:
            job.exit_code = exit_code
        payload = {"type": "status", "data": status}
        if exit_code is not None:
            payload["exit_code"] = exit_code
        self._broadcast(job.ws_clients, payload)

    def set_run_status(self, run: RunSession, status: str, info: dict | None = None) -> None:
        run.status = status
        payload = {"type": "status", "data": status}
        if info:
            payload["info"] = info
        self._broadcast(run.ws_clients, payload)

    def _broadcast(self, clients: Set[WebSocket], payload: dict) -> None:
        if not clients:
            return
        if self._loop is None or not self._loop.is_running():
            return

        async def _send_all() -> None:
            dead: list[WebSocket] = []
            for ws in list(clients):
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                clients.discard(ws)

        asyncio.run_coroutine_threadsafe(_send_all(), self._loop)

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self.sessions.values())
            deploys = list(self.deploy_jobs.values())
            runs = list(self.run_sessions.values())
        sessions_by_id = {session.session_id: session for session in sessions}
        for job in deploys:
            job.cancel_event.set()
            if job.channel is not None:
                try:
                    job.channel.close()
                except Exception:
                    pass
        try:
            from .run_service import stop_run
        except Exception:
            stop_run = None
        for run in runs:
            session = sessions_by_id.get(run.session_id)
            if stop_run is not None and session is not None:
                try:
                    stop_run(self, run, session)
                    continue
                except Exception:
                    pass
            if run.tunnel is not None:
                try:
                    run.tunnel.stop()
                except Exception:
                    pass
                run.tunnel = None
            run.local_port = None
            if session is not None:
                try:
                    from .run_service import _stop_remote
                except Exception:
                    _stop_remote = None
                if _stop_remote is not None:
                    try:
                        _stop_remote(session.ssh, run.remote_pid)
                    except Exception:
                        pass
        for session in sessions:
            try:
                session.ssh.close()
            except Exception:
                pass
