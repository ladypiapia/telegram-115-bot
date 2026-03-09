from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aria2p
import requests

from src.config import Aria2Settings


@dataclass(slots=True)
class Aria2Task:
    gid: str
    local_path: Path
    file_name: str


class Aria2RPCService:
    def __init__(self, settings: Aria2Settings) -> None:
        self.settings = settings
        self.api: aria2p.API | None = None
        if settings.enable:
            self.api = aria2p.API(
                aria2p.Client(
                    host=settings.host,
                    port=settings.port,
                    secret=settings.rpc_secret,
                )
            )

    def ensure_enabled(self) -> None:
        if not self.settings.enable or self.api is None:
            raise RuntimeError("aria2 is disabled or not configured")

    def get_version(self, timeout: float = 15) -> dict[str, Any]:
        self.ensure_enabled()
        response = requests.post(
            self._rpc_url(),
            json={
                "jsonrpc": "2.0",
                "id": "telegram-115-bot",
                "method": "aria2.getVersion",
                "params": self._rpc_params(),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"aria2 getVersion failed: {payload['error']}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"aria2 getVersion returned unexpected payload: {payload}")
        return result

    def add_download(self, download_url: str, target_dir: str | Path, file_name: str) -> Aria2Task:
        self.ensure_enabled()
        directory = Path(target_dir)
        directory.mkdir(parents=True, exist_ok=True)
        options = {
            "dir": str(directory),
            "out": file_name,
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
            "continue": "true",
        }
        download = self.api.add(download_url, options=options)
        return Aria2Task(
            gid=download.gid,
            local_path=directory / file_name,
            file_name=file_name,
        )

    def get_status(self, gid: str) -> dict[str, Any]:
        self.ensure_enabled()
        download = self.api.get_download(gid)
        return {
            "gid": gid,
            "status": download.status,
            "name": download.name,
            "progress": getattr(download, "progress", "0%"),
            "completed_length": download.completed_length,
            "total_length": download.total_length,
            "download_speed": download.download_speed,
            "error_message": download.error_message,
        }

    def _rpc_url(self) -> str:
        raw_host = self.settings.host or "http://127.0.0.1"
        normalized = raw_host if "://" in raw_host else f"http://{raw_host}"
        parsed = urlparse(normalized)
        scheme = parsed.scheme or "http"
        hostname = parsed.hostname or "127.0.0.1"
        port = parsed.port or self.settings.port
        return f"{scheme}://{hostname}:{port}/jsonrpc"

    def _rpc_params(self) -> list[str]:
        if not self.settings.rpc_secret:
            return []
        return [f"token:{self.settings.rpc_secret}"]
