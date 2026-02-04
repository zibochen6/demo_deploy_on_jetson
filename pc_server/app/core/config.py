from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[3]
DEMOS_JSON = ROOT_DIR / "demos.json"

LOG_BUFFER_LINES = 500
DEPLOY_READ_CHUNK = 4096
RUN_READ_CHUNK = 65536
DEPLOY_STATUS_TIMEOUT = 10
RUN_HEALTH_TIMEOUT = 40
RUN_HEALTH_INTERVAL = 1.0
SSH_CONNECT_TIMEOUT = 15
SSH_BANNER_TIMEOUT = 15


@dataclass
class DemoConfig:
    raw: dict

    @property
    def id(self) -> str:
        return self.raw.get("id", "")

    @property
    def name(self) -> str:
        return self.raw.get("name", "")

    @property
    def description(self) -> str:
        return self.raw.get("description", "")

    @property
    def tags(self) -> list[str]:
        return list(self.raw.get("tags", []) or [])

    @property
    def media(self) -> list[dict]:
        return list(self.raw.get("media", []) or [])

    @property
    def deploy(self) -> dict:
        return self.raw.get("deploy", {})

    @property
    def run(self) -> dict:
        return self.raw.get("run", {})

    @property
    def status(self) -> dict:
        return self.raw.get("status", {})


class DemoRegistry:
    def __init__(self, demos: list[dict[str, Any]]):
        self._demos = {d.get("id"): DemoConfig(d) for d in demos}

    def list(self) -> list[DemoConfig]:
        return list(self._demos.values())

    def get(self, demo_id: str) -> DemoConfig | None:
        return self._demos.get(demo_id)


def load_registry() -> DemoRegistry:
    if not DEMOS_JSON.exists():
        raise RuntimeError(f"demos.json not found: {DEMOS_JSON}")
    with DEMOS_JSON.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    demos = data.get("demos", [])
    if not isinstance(demos, list):
        raise RuntimeError("demos.json: demos must be a list")
    return DemoRegistry(demos)


def resolve_path(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return ROOT_DIR / p
