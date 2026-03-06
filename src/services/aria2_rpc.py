from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aria2p

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
