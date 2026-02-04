from __future__ import annotations

import re
import socket
from collections import deque
from typing import Deque, Iterable


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_log_line(line: str) -> str:
    if not line:
        return ""
    cleaned = ANSI_ESCAPE_RE.sub("", line)
    cleaned = CONTROL_CHARS_RE.sub("", cleaned)
    return cleaned


class LineBuffer:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buf += chunk
        lines = self._buf.splitlines(keepends=True)
        if not lines:
            return []
        completed: list[str] = []
        if lines[-1].endswith("\n") or lines[-1].endswith("\r"):
            use = lines
            self._buf = ""
        else:
            use = lines[:-1]
            self._buf = lines[-1]
        for line in use:
            completed.append(line.rstrip("\r\n"))
        return completed

    def flush(self) -> list[str]:
        if not self._buf:
            return []
        last = self._buf
        self._buf = ""
        return [last]


class RingBuffer:
    def __init__(self, maxlen: int = 200) -> None:
        self._buf: Deque[str] = deque(maxlen=maxlen)

    def append(self, item: str) -> None:
        self._buf.append(item)

    def extend(self, items: Iterable[str]) -> None:
        for item in items:
            self.append(item)

    def list(self) -> list[str]:
        return list(self._buf)


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]
