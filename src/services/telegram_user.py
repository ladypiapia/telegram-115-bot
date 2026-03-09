from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import socks
from telethon import TelegramClient
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError

from src.config import Settings


class TelegramUserService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = TelegramClient(
            str(settings.session_file),
            settings.tg_api_id,
            settings.tg_api_hash,
            proxy=self._build_proxy(),
        )

    def _build_proxy(self) -> tuple | None:
        raw_proxy = self.settings.proxy.http or self.settings.proxy.https
        if not raw_proxy:
            return None
        normalized = raw_proxy if "://" in raw_proxy else f"http://{raw_proxy}"
        parsed = urlparse(normalized)
        if not parsed.hostname or not parsed.port:
            return None
        proxy_type = socks.HTTP if parsed.scheme.startswith("http") else socks.SOCKS5
        return (
            proxy_type,
            parsed.hostname,
            parsed.port,
            True,
            parsed.username,
            parsed.password,
        )

    async def start(self) -> None:
        try:
            await self.client.connect()
        except AuthKeyDuplicatedError as exc:
            raise RuntimeError(
                "Telethon 会话已失效：同一个 user_session.session 曾在不同出口 IP 上同时使用，"
                "Telegram 已废弃这份授权。请停止其他仍在使用该 session 的程序，删除当前 "
                f"session 文件后重新登录生成新会话：{self.settings.session_file}"
            ) from exc

    async def stop(self) -> None:
        await self.client.disconnect()

    async def ensure_authorized(self) -> bool:
        if not self.client.is_connected():
            await self.client.connect()
        return await self.client.is_user_authorized()

    def _resolve_source_entity(self, chat_id: int, user_id: int) -> str | int:
        return self.settings.bot_name if chat_id == user_id else chat_id

    def _resolve_send_entity(self, chat_id: int, user_id: int) -> str | int:
        return "me" if chat_id == user_id else chat_id

    async def download_bot_media(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        destination: str | Path,
    ) -> Path:
        entity = self._resolve_source_entity(chat_id, user_id)
        message = await self.client.get_messages(entity, ids=message_id)
        if not message or not message.media:
            raise RuntimeError("Unable to fetch source media from Telegram")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        saved = await self.client.download_media(message, file=str(target))
        if not saved:
            raise RuntimeError("Telegram media download failed")
        return Path(saved)

    async def send_file(
        self,
        chat_id: int,
        user_id: int,
        file_path: str | Path,
        caption: str | None = None,
        progress_callback: Callable[[int, int], object] | None = None,
    ) -> str:
        entity = self._resolve_send_entity(chat_id, user_id)
        await self.client.send_file(
            entity,
            file=str(file_path),
            caption=caption,
            force_document=True,
            progress_callback=progress_callback,
        )
        return "Saved Messages" if entity == "me" else str(chat_id)
