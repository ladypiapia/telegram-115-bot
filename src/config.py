from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class PathOption:
    name: str
    path: str


@dataclass(slots=True)
class CategoryFolder:
    name: str
    display_name: str
    path_map: list[PathOption]


@dataclass(slots=True)
class ProxySettings:
    http: str
    https: str
    no_proxy: str


@dataclass(slots=True)
class OfflineSettings:
    poll_interval: int
    timeout: int


@dataclass(slots=True)
class Aria2Settings:
    enable: bool
    host: str
    port: int
    rpc_secret: str
    download_path: str
    poll_interval: int


@dataclass(slots=True)
class UploadSettings:
    temp_dir: str


@dataclass(slots=True)
class Settings:
    bot_token: str
    allowed_user: str
    bot_name: str
    tg_api_id: int
    tg_api_hash: str
    app_id_115: str
    access_token: str
    refresh_token: str
    category_folder: list[CategoryFolder]
    proxy: ProxySettings
    offline: OfflineSettings
    aria2: Aria2Settings
    upload: UploadSettings
    config_path: Path
    project_root: Path

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    @property
    def token_file(self) -> Path:
        return self.config_dir / "115_tokens.json"

    @property
    def session_file(self) -> Path:
        return self.config_dir / "user_session.session"

    def is_allowed(self, user_id: int) -> bool:
        return str(user_id) == self.allowed_user

    def apply_proxy_env(self) -> None:
        if self.proxy.http:
            os.environ["HTTP_PROXY"] = self.proxy.http
        if self.proxy.https:
            os.environ["HTTPS_PROXY"] = self.proxy.https
        if self.proxy.no_proxy:
            os.environ["NO_PROXY"] = self.proxy.no_proxy

    def ensure_directories(self) -> None:
        Path(self.upload.temp_dir).mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)


def _require(data: dict[str, Any], key: str, default: Any = None) -> Any:
    value = data.get(key, default)
    if value is None:
        raise ValueError(f"Missing config key: {key}")
    return value


def _load_categories(raw_categories: list[dict[str, Any]]) -> list[CategoryFolder]:
    categories: list[CategoryFolder] = []
    for raw_category in raw_categories:
        path_map = [
            PathOption(name=str(item["name"]), path=str(item["path"]))
            for item in raw_category.get("path_map", [])
        ]
        categories.append(
            CategoryFolder(
                name=str(raw_category["name"]),
                display_name=str(raw_category["display_name"]),
                path_map=path_map,
            )
        )
    return categories


def load_settings(config_path: str | Path | None = None) -> Settings:
    config_file = Path(config_path or "config/config.yaml").expanduser().resolve()
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    with config_file.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    project_root = config_file.parent.parent
    category_folder = _load_categories(raw.get("category_folder", []))
    if not category_folder:
        raise ValueError("category_folder must not be empty")

    proxy = raw.get("proxy", {})
    offline = raw.get("offline", {})
    aria2 = raw.get("aria2", {})
    upload = raw.get("upload", {})

    settings = Settings(
        bot_token=str(_require(raw, "bot_token")),
        allowed_user=str(_require(raw, "allowed_user")),
        bot_name=str(_require(raw, "bot_name")),
        tg_api_id=int(_require(raw, "tg_api_id")),
        tg_api_hash=str(_require(raw, "tg_api_hash")),
        app_id_115=str(raw.get("115_app_id", "") or ""),
        access_token=str(raw.get("access_token", "") or ""),
        refresh_token=str(raw.get("refresh_token", "") or ""),
        category_folder=category_folder,
        proxy=ProxySettings(
            http=str(proxy.get("http", "http://127.0.0.1:7890") or ""),
            https=str(proxy.get("https", "http://127.0.0.1:7890") or ""),
            no_proxy=str(proxy.get("no_proxy", "localhost,127.0.0.1,::1,.115.com") or ""),
        ),
        offline=OfflineSettings(
            poll_interval=int(offline.get("poll_interval", 10)),
            timeout=int(offline.get("timeout", 900)),
        ),
        aria2=Aria2Settings(
            enable=bool(aria2.get("enable", False)),
            host=str(aria2.get("host", "")),
            port=int(aria2.get("port", 6800)),
            rpc_secret=str(aria2.get("rpc_secret", "")),
            download_path=str(aria2.get("download_path", "/downloads")),
            poll_interval=int(aria2.get("poll_interval", 5)),
        ),
        upload=UploadSettings(
            temp_dir=str(upload.get("temp_dir", str(project_root / "tmp"))),
        ),
        config_path=config_file,
        project_root=project_root,
    )

    if not settings.app_id_115 and not (settings.access_token and settings.refresh_token):
        raise ValueError("Configure either 115_app_id or access_token/refresh_token")
    return settings
