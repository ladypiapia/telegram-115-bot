from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut

from src.config import CategoryFolder, Settings
from src.services.aria2_rpc import Aria2RPCService, Aria2Task
from src.services.av_search import AVSearchService
from src.services.open115 import AuthSession, Open115APIError, Open115Client, RemoteFile
from src.services.telegram_user import TelegramUserService


logger = logging.getLogger(__name__)
MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:[^ ]+", re.IGNORECASE)


@dataclass(slots=True)
class PendingSelection:
    selection_id: str
    chat_id: int
    user_id: int
    kind: str
    payload: dict[str, Any]
    option_map: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class MessageRef:
    chat_id: int
    user_id: int
    message_id: int
    file_name: str
    file_id: str


class TaskFlowService:
    def __init__(
        self,
        settings: Settings,
        open115: Open115Client,
        aria2: Aria2RPCService,
        telegram_user: TelegramUserService,
        av_search: AVSearchService,
    ) -> None:
        self.settings = settings
        self.open115 = open115
        self.aria2 = aria2
        self.telegram_user = telegram_user
        self.av_search = av_search
        self.bot: Bot | None = None
        self.pending: dict[str, PendingSelection] = {}
        self.last_save_path: dict[int, str] = {}
        self.active_tasks: dict[str, asyncio.Task] = {}

    def bind_bot(self, bot: Bot) -> None:
        self.bot = bot

    def is_magnet(self, text: str) -> bool:
        return bool(MAGNET_RE.match(text.strip()))

    def create_selection(self, *, chat_id: int, user_id: int, kind: str, payload: dict[str, Any]) -> PendingSelection:
        selection = PendingSelection(
            selection_id=uuid.uuid4().hex[:10],
            chat_id=chat_id,
            user_id=user_id,
            kind=kind,
            payload=payload,
        )
        self.pending[selection.selection_id] = selection
        return selection

    def pop_selection(self, selection_id: str) -> PendingSelection | None:
        return self.pending.pop(selection_id, None)

    def get_selection(self, selection_id: str) -> PendingSelection | None:
        return self.pending.get(selection_id)

    def clear_chat_pending(self, chat_id: int) -> None:
        stale_ids = [selection_id for selection_id, item in self.pending.items() if item.chat_id == chat_id]
        for selection_id in stale_ids:
            self.pending.pop(selection_id, None)

    def build_main_keyboard(self, selection: PendingSelection) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton(f"📁 {category.display_name}", callback_data=f"selm:{selection.selection_id}:{category.name}")]
            for category in self.settings.category_folder
        ]
        last_path = self.last_save_path.get(selection.chat_id)
        if last_path:
            buttons.append([InlineKeyboardButton(f"🚀 上次保存: {last_path}", callback_data=f"sellast:{selection.selection_id}")])
        buttons.append([InlineKeyboardButton("取消", callback_data=f"selc:{selection.selection_id}")])
        return InlineKeyboardMarkup(buttons)

    def build_sub_keyboard(self, selection: PendingSelection, category_name: str) -> InlineKeyboardMarkup:
        category = self._find_category(category_name)
        if not category:
            raise KeyError(category_name)
        selection.option_map.clear()
        buttons = []
        for index, item in enumerate(category.path_map):
            option_id = f"p{index}"
            selection.option_map[option_id] = item.path
            buttons.append([InlineKeyboardButton(f"📁 {item.name}", callback_data=f"sell:{selection.selection_id}:{option_id}")])
        buttons.append([InlineKeyboardButton("取消", callback_data=f"selc:{selection.selection_id}")])
        return InlineKeyboardMarkup(buttons)

    def resolve_save_path(self, selection: PendingSelection, option_id: str) -> str:
        if option_id not in selection.option_map:
            raise KeyError(option_id)
        return selection.option_map[option_id]

    async def run_auth(self, chat_id: int, auth_session: AuthSession) -> None:
        try:
            await asyncio.to_thread(self.open115.wait_for_auth, auth_session)
            await self.notify(chat_id, "115 授权成功。")
        except Exception as exc:
            logger.exception("115 auth failed")
            await self.notify(chat_id, f"115 授权失败: {exc}")
        finally:
            try:
                auth_session.qr_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Unable to delete auth QR: %s", auth_session.qr_path)

    async def start_magnet_task(self, *, chat_id: int, user_id: int, magnet: str, save_path: str, label: str) -> None:
        task_id = uuid.uuid4().hex[:10]
        task = asyncio.create_task(self._offline_to_telegram(task_id, chat_id, user_id, magnet, save_path, label))
        self._track_task(task_id, task)

    async def start_av_task(self, *, chat_id: int, user_id: int, query: str, save_path: str) -> None:
        task_id = uuid.uuid4().hex[:10]
        task = asyncio.create_task(self._av_to_telegram(task_id, chat_id, user_id, query, save_path))
        self._track_task(task_id, task)

    async def start_upload_task(self, *, ref: MessageRef, save_path: str) -> None:
        task_id = uuid.uuid4().hex[:10]
        task = asyncio.create_task(self._telegram_file_to_115(task_id, ref, save_path))
        self._track_task(task_id, task)

    async def _av_to_telegram(self, task_id: str, chat_id: int, user_id: int, query: str, save_path: str) -> None:
        await self.notify(chat_id, f"正在搜索 {query} 的磁力链接。")
        try:
            results = await asyncio.to_thread(self.av_search.search, query)
        except Exception as exc:
            await self.notify(chat_id, f"/av 搜索失败: {exc}")
            return
        if not results:
            await self.notify(chat_id, f"没有找到 {query} 的可用磁力。")
            return

        last_error: Exception | None = None
        for result in results:
            try:
                await self._offline_to_telegram(task_id, chat_id, user_id, result.magnet, save_path, query, quiet_start=True)
                return
            except Exception as exc:
                last_error = exc
                logger.warning("AV candidate failed for %s: %s", query, exc)

        await self.notify(chat_id, f"{query} 的所有候选磁力均失败: {last_error}")

    async def _offline_to_telegram(
        self,
        task_id: str,
        chat_id: int,
        user_id: int,
        magnet: str,
        save_path: str,
        label: str,
        *,
        quiet_start: bool = False,
    ) -> None:
        if not self.aria2.settings.enable:
            raise RuntimeError("aria2 未启用，无法完成自动推送链路")

        if not quiet_start:
            await self.notify(chat_id, f"已接收任务：{label}\n正在提交到 115 离线。")

        try:
            await asyncio.to_thread(self.open115.add_offline_task, magnet, save_path)
            task_info = await asyncio.to_thread(
                self.open115.wait_offline_complete,
                magnet,
                self.settings.offline.timeout,
                self.settings.offline.poll_interval,
            )
            root_name = task_info.name or "offline-task"
            resource_path = f"{save_path.rstrip('/')}/{root_name}"
            files = await asyncio.to_thread(self.open115.list_downloadable_files, resource_path)
            if not files:
                raise RuntimeError("115 离线完成后没有发现可下载文件")

            pushed: list[Aria2Task] = []
            root_dir = Path(self.settings.aria2.download_path) / _safe_name(root_name)
            for remote_file in files:
                download_url = await asyncio.to_thread(self.open115.get_download_url, remote_file.pick_code)
                relative_parent = Path(remote_file.relative_path).parent
                local_dir = root_dir if str(relative_parent) == "." else root_dir / relative_parent
                aria2_task = await asyncio.to_thread(
                    self.aria2.add_download,
                    download_url,
                    local_dir,
                    remote_file.name,
                )
                pushed.append(aria2_task)
                child_task = asyncio.create_task(
                    self._wait_aria2_and_send(chat_id, user_id, aria2_task, root_dir)
                )
                self._track_task(f"{task_id}-{aria2_task.gid}", child_task)

            await self.notify(
                chat_id,
                f"115 离线完成：{root_name}\n已推送 {len(pushed)} 个文件到 aria2。",
            )
        except Exception as exc:
            if quiet_start:
                raise
            logger.exception("offline pipeline failed")
            await self.notify(chat_id, f"任务失败：{label}\n原因：{_describe_failure_reason(save_path, exc)}")
            raise

    async def _wait_aria2_and_send(
        self,
        chat_id: int,
        user_id: int,
        aria2_task: Aria2Task,
        root_dir: Path,
    ) -> None:
        try:
            while True:
                status = await asyncio.to_thread(self.aria2.get_status, aria2_task.gid)
                if status["status"] == "complete":
                    break
                if status["status"] in {"error", "removed"}:
                    raise RuntimeError(status.get("error_message") or "aria2 下载失败")
                await asyncio.sleep(self.settings.aria2.poll_interval)

            if not aria2_task.local_path.exists():
                raise FileNotFoundError(f"aria2 下载完成，但找不到文件: {aria2_task.local_path}")

            target_label = await self.telegram_user.send_file(chat_id, user_id, aria2_task.local_path)
            aria2_task.local_path.unlink(missing_ok=True)
            _cleanup_empty_dirs(aria2_task.local_path.parent, root_dir)
            await self.notify(chat_id, f"已发送文件 {aria2_task.file_name} 到 {target_label}。")
        except Exception as exc:
            logger.exception("aria2 send pipeline failed")
            await self.notify(chat_id, f"文件回传失败 {aria2_task.file_name}: {exc}")

    async def _telegram_file_to_115(self, task_id: str, ref: MessageRef, save_path: str) -> None:
        temp_name = f"{uuid.uuid4().hex[:8]}-{ref.file_name}"
        temp_path = Path(self.settings.upload.temp_dir) / temp_name
        await self.notify(ref.chat_id, f"正在下载 Telegram 文件 {ref.file_name}。")
        try:
            if not self.bot:
                raise RuntimeError("bot is not bound")
            tg_file = await self.bot.get_file(ref.file_id)
            downloaded_file = await tg_file.download_to_drive(custom_path=temp_path)
            await self.notify(ref.chat_id, f"正在上传 {downloaded_file.name} 到 115。")
            uploaded, instant = await asyncio.to_thread(self.open115.upload_file, downloaded_file, save_path)
            if not uploaded:
                raise RuntimeError("115 上传失败")
            result_text = "秒传成功" if instant else "上传成功"
            await self.notify(ref.chat_id, f"{result_text}: {downloaded_file.name}\n保存目录: {save_path}")
        except Exception as exc:
            logger.exception("telegram upload pipeline failed")
            await self.notify(ref.chat_id, f"发送到 115 失败: {exc}")
        finally:
            temp_path.unlink(missing_ok=True)

    async def notify(self, chat_id: int, text: str) -> None:
        if not self.bot:
            raise RuntimeError("bot is not bound")
        for attempt in range(3):
            try:
                await self.bot.send_message(chat_id=chat_id, text=text)
                return
            except (TimedOut, NetworkError):
                if attempt == 2:
                    raise
                await asyncio.sleep(1 + attempt)

    def _find_category(self, category_name: str) -> CategoryFolder | None:
        for category in self.settings.category_folder:
            if category.name == category_name:
                return category
        return None

    def _track_task(self, task_id: str, task: asyncio.Task) -> None:
        self.active_tasks[task_id] = task
        task.add_done_callback(self._cleanup_task(task_id))

    def _cleanup_task(self, task_id: str):
        def _callback(task: asyncio.Task) -> None:
            self.active_tasks.pop(task_id, None)
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                logger.error("Background task %s failed: %s", task_id, exc)

        return _callback


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "download"


def _describe_failure_reason(save_path: str, exc: Exception) -> str:
    if isinstance(exc, Open115APIError):
        if exc.code == 10008:
            return f"115 提示该离线任务已存在，请勿重复提交。原始消息：{exc.api_message}。保存目录：{save_path}"
        return f"115 接口异常：{exc.api_message}"
    return str(exc) or exc.__class__.__name__


def _cleanup_empty_dirs(start_dir: Path, stop_dir: Path) -> None:
    current = start_dir
    stop_dir = stop_dir.resolve()
    while current.exists():
        try:
            if current.resolve() == stop_dir.resolve():
                if not any(current.iterdir()):
                    current.rmdir()
                break
            if any(current.iterdir()):
                break
            current.rmdir()
            current = current.parent
        except OSError:
            break
