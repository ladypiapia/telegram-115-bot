from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
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


@dataclass(slots=True)
class PollingHealth:
    check_interval: int
    failure_threshold: int
    max_restart_failures: int
    consecutive_failures: int = 0
    restart_failures: int = 0
    poll_error_count: int = 0
    last_api_ok_at: float = 0.0
    last_poll_error_at: float = 0.0
    last_error: str = ""
    restart_in_progress: bool = False


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

    polling_health = PollingHealth(
        check_interval=int(os.getenv("BOT_WATCHDOG_INTERVAL", "30")),
        failure_threshold=int(os.getenv("BOT_WATCHDOG_FAILURE_THRESHOLD", "3")),
        max_restart_failures=int(os.getenv("BOT_WATCHDOG_MAX_RESTART_FAILURES", "3")),
        last_api_ok_at=time.monotonic(),
    )
    polling_kwargs = {
        "timeout": 60,
        "bootstrap_retries": 3,
        "error_callback": _make_polling_error_callback(polling_health),
    }
    watchdog_task: asyncio.Task | None = None
    keepalive_task: asyncio.Task | None = None

    try:
        await application.initialize()
        await application.start()
        allowed_chat_id = int(settings.allowed_user)
        await register_bot_commands(application, chat_id=allowed_chat_id)
        await _notify_startup(flow, allowed_chat_id, user_info)
        await application.updater.start_polling(**polling_kwargs)
        watchdog_task = asyncio.create_task(
            _polling_watchdog(application, flow, allowed_chat_id, polling_health, polling_kwargs),
            name="polling-watchdog",
        )
        keepalive_task = asyncio.create_task(asyncio.Event().wait(), name="main-keepalive")
        done, pending = await asyncio.wait(
            {watchdog_task, keepalive_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
    finally:
        if keepalive_task:
            keepalive_task.cancel()
        if watchdog_task:
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)
        if application.updater.running:
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


def _make_polling_error_callback(polling_health: PollingHealth):
    def _callback(exc) -> None:
        polling_health.poll_error_count += 1
        polling_health.last_poll_error_at = time.monotonic()
        polling_health.last_error = f"{exc.__class__.__name__}: {exc}"
        logging.getLogger(__name__).warning(
            "Telegram polling error #%s: %s",
            polling_health.poll_error_count,
            polling_health.last_error,
        )

    return _callback


async def _polling_watchdog(
    application: Application,
    flow: TaskFlowService,
    chat_id: int,
    polling_health: PollingHealth,
    polling_kwargs: dict,
) -> None:
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(polling_health.check_interval)
        try:
            await application.bot.get_me()
        except Exception as exc:
            polling_health.consecutive_failures += 1
            polling_health.last_error = f"{exc.__class__.__name__}: {exc}"
            logger.warning(
                "Bot API healthcheck failed (%s/%s): %s",
                polling_health.consecutive_failures,
                polling_health.failure_threshold,
                polling_health.last_error,
            )
            if polling_health.consecutive_failures < polling_health.failure_threshold:
                continue
            if polling_health.restart_in_progress:
                continue
            polling_health.restart_in_progress = True
            try:
                await _restart_updater(application, polling_kwargs)
                await application.bot.get_me()
            except Exception as restart_exc:
                polling_health.restart_failures += 1
                logger.exception(
                    "Updater self-heal failed (%s/%s)",
                    polling_health.restart_failures,
                    polling_health.max_restart_failures,
                )
                if polling_health.restart_failures >= polling_health.max_restart_failures:
                    raise RuntimeError(
                        "Telegram polling could not recover after repeated restart attempts"
                    ) from restart_exc
            else:
                recovered_after = polling_health.consecutive_failures
                logger.warning("Updater polling recovered after self-heal restart")
                polling_health.consecutive_failures = 0
                polling_health.restart_failures = 0
                polling_health.last_api_ok_at = time.monotonic()
                await _notify_watchdog_event(
                    flow,
                    chat_id,
                    f"Bot 轮询已自动恢复，已重启 updater。\n恢复前连续失败次数：{recovered_after}",
                )
            finally:
                polling_health.restart_in_progress = False
        else:
            if polling_health.consecutive_failures:
                recovered_after = polling_health.consecutive_failures
                logger.info("Bot API healthcheck recovered after %s failure(s)", recovered_after)
                await _notify_watchdog_event(
                    flow,
                    chat_id,
                    f"Bot 网络已恢复。\n恢复前连续失败次数：{recovered_after}",
                )
            polling_health.consecutive_failures = 0
            polling_health.restart_failures = 0
            polling_health.last_api_ok_at = time.monotonic()


async def _restart_updater(application: Application, polling_kwargs: dict) -> None:
    if application.updater.running:
        await application.updater.stop()
    await application.updater.start_polling(**polling_kwargs)


async def _notify_watchdog_event(flow: TaskFlowService, chat_id: int, text: str) -> None:
    try:
        await flow.notify(chat_id, text)
    except Exception:
        logging.getLogger(__name__).exception("Failed to send watchdog notification")


if __name__ == "__main__":
    asyncio.run(main())
