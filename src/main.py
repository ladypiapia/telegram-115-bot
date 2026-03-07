from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from telegram.ext import Application

from src.bot.handlers import post_init, register_bot_commands, register_handlers
from src.config import Settings, load_settings
from src.services.aria2_rpc import Aria2RPCService
from src.services.av_search import AVSearchService
from src.services.open115 import Open115Client
from src.services.task_flow import TaskFlowService
from src.services.telegram_user import TelegramUserService


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def main() -> None:
    configure_logging()
    default_config_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    config_path = Path(os.getenv("BOT_CONFIG", str(default_config_path))).expanduser()
    settings = load_settings(config_path)
    settings.apply_proxy_env()
    settings.ensure_directories()

    open115 = Open115Client(settings)
    user_info = open115.get_user_info()

    telegram_user = TelegramUserService(settings)
    await telegram_user.start()
    if not await telegram_user.ensure_authorized():
        raise RuntimeError(f"Telethon session is not authorized: {settings.session_file}")

    aria2 = Aria2RPCService(settings.aria2)
    av_search = AVSearchService()
    flow = TaskFlowService(settings, open115, aria2, telegram_user, av_search)

    builder = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(post_init)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(20)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(60)
        .get_updates_pool_timeout(20)
    )
    bot_proxy = settings.proxy.https or settings.proxy.http
    if bot_proxy:
        builder = builder.proxy(bot_proxy).get_updates_proxy(bot_proxy)
    application = builder.build()
    flow.bind_bot(application.bot)
    register_handlers(application, settings, flow, open115)

    try:
        await application.initialize()
        await application.start()
        allowed_chat_id = int(settings.allowed_user)
        await register_bot_commands(application, chat_id=allowed_chat_id)
        await _notify_startup(flow, allowed_chat_id, user_info)
        await application.updater.start_polling()
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await telegram_user.stop()


async def _notify_startup(flow: TaskFlowService, chat_id: int, user_info: dict) -> None:
    try:
        await flow.notify(chat_id, _format_startup_message(user_info))
    except Exception:
        logging.getLogger(__name__).exception("Failed to send startup notification")


def _format_startup_message(user_info: dict) -> str:
    lines = ["机器人启动成功。", "115 用户信息："]
    for label, value in (
        ("用户 ID", _pick_first(user_info, "user_id", "uid")),
        ("昵称", _pick_first(user_info, "user_name", "nick_name", "nickname")),
        ("手机号", _pick_first(user_info, "mobile")),
        ("会员", _pick_first(user_info, "vip", "is_vip")),
        ("到期时间", _pick_first(user_info, "vip_end_time", "vip_expire", "expire_time")),
    ):
        if value not in (None, "", []):
            lines.append(f"{label}: {_stringify(value)}")

    if len(lines) == 2:
        lines.append(json.dumps(user_info, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def _pick_first(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _stringify(value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


if __name__ == "__main__":
    asyncio.run(main())
