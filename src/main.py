from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from telegram.ext import Application

from src.bot.handlers import post_init, register_handlers
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
    config_path = Path(os.getenv("BOT_CONFIG", "config/config.yaml"))
    settings = load_settings(config_path)
    settings.apply_proxy_env()
    settings.ensure_directories()

    open115 = Open115Client(settings)
    open115.get_user_info()

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
        await application.updater.start_polling()
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await telegram_user.stop()


if __name__ == "__main__":
    asyncio.run(main())
