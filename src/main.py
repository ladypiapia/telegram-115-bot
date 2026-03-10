from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from telegram.ext import Application, ExtBot
from telegram.request import HTTPXRequest

from src.bot.handlers import post_init, register_bot_commands, register_handlers
from src.config import Settings, load_settings
from src.runtime import RuntimeHealth
from src.services.aria2_rpc import Aria2RPCService
from src.services.av_search import AVSearchService
from src.services.open115 import Open115Client, Open115TemporaryError
from src.services.task_flow import TaskFlowService
from src.services.telegram_user import TelegramUserService
from src.systemd_notify import SystemdNotifier


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


class TrackingExtBot(ExtBot):
    def __init__(self, *args, runtime_health: RuntimeHealth, **kwargs) -> None:
        self._runtime_health = runtime_health
        super().__init__(*args, **kwargs)

    async def get_updates(self, *args, **kwargs):
        self._runtime_health.mark_get_updates_start()
        try:
            updates = await super().get_updates(*args, **kwargs)
        except Exception as exc:
            self._runtime_health.last_error = f"get_updates failed: {exc.__class__.__name__}: {exc}"
            raise
        else:
            self._runtime_health.mark_get_updates_end()
            return updates
        finally:
            if self._runtime_health.get_updates_in_progress:
                self._runtime_health.get_updates_in_progress = False


async def main() -> None:
    configure_logging()
    default_config_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    config_path = Path(os.getenv("BOT_CONFIG", str(default_config_path))).expanduser()
    settings = load_settings(config_path)
    settings.apply_proxy_env()
    settings.ensure_directories()
    _log_proxy_summary(settings)

    runtime_health = RuntimeHealth(
        check_interval=int(os.getenv("BOT_WATCHDOG_INTERVAL", "15")),
        failure_threshold=int(os.getenv("BOT_WATCHDOG_FAILURE_THRESHOLD", "3")),
        max_restart_failures=int(os.getenv("BOT_WATCHDOG_MAX_RESTART_FAILURES", "3")),
        get_updates_stuck_timeout=int(os.getenv("BOT_GET_UPDATES_STUCK_TIMEOUT", "180")),
        handler_timeout=int(os.getenv("BOT_HANDLER_TIMEOUT", "120")),
        blocking_stage_timeout=int(os.getenv("BOT_BLOCKING_STAGE_TIMEOUT", "300")),
        aria2_rpc_timeout=int(os.getenv("BOT_ARIA2_RPC_TIMEOUT", "15")),
        telegram_transfer_timeout=int(os.getenv("BOT_TELEGRAM_TRANSFER_TIMEOUT", "3600")),
        telethon_stall_timeout=int(os.getenv("BOT_TELETHON_STALL_TIMEOUT", "300")),
        last_api_ok_at=time.monotonic(),
        last_progress_at=time.monotonic(),
    )
    notifier = SystemdNotifier()

    open115 = Open115Client(settings)
    user_info = await _load_startup_user_info(open115, runtime_health)

    telegram_user = TelegramUserService(settings)
    await telegram_user.start()
    if not await telegram_user.ensure_authorized():
        raise RuntimeError(f"Telethon session is not authorized: {settings.session_file}")
    runtime_health.mark_progress("telethon bootstrap ok")

    aria2 = Aria2RPCService(settings.aria2)
    av_search = AVSearchService()
    flow = TaskFlowService(settings, open115, aria2, telegram_user, av_search, runtime_health)
    bot_proxy = settings.proxy.https or settings.proxy.http
    request = HTTPXRequest(
        connection_pool_size=256,
        connect_timeout=20,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=20,
        proxy=bot_proxy or None,
    )
    get_updates_request = HTTPXRequest(
        connection_pool_size=int(os.getenv("BOT_GET_UPDATES_POOL_SIZE", "2")),
        connect_timeout=20,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=20,
        proxy=bot_proxy or None,
    )
    builder = (
        Application.builder()
        .bot(
            TrackingExtBot(
                token=settings.bot_token,
                request=request,
                get_updates_request=get_updates_request,
                runtime_health=runtime_health,
            )
        )
        .post_init(post_init)
    )
    application = builder.build()
    flow.bind_bot(application.bot)
    register_handlers(application, settings, flow, open115)

    polling_kwargs = {
        "timeout": 60,
        "bootstrap_retries": 3,
        "error_callback": _make_polling_error_callback(runtime_health),
    }
    watchdog_task: asyncio.Task | None = None
    fatal_wait_task: asyncio.Task | None = None
    systemd_watchdog_task: asyncio.Task | None = None

    try:
        await application.initialize()
        await application.start()
        await _startup_probe(application, aria2, runtime_health)
        allowed_chat_id = int(settings.allowed_user)
        await register_bot_commands(application, chat_id=allowed_chat_id)
        await _notify_startup(flow, allowed_chat_id, user_info)
        await application.updater.start_polling(**polling_kwargs)
        runtime_health.mark_progress("updater polling started")
        if notifier.enabled:
            notifier.ready("telegram-115-bot running")
        watchdog_task = asyncio.create_task(
            _polling_watchdog(application, flow, allowed_chat_id, runtime_health, polling_kwargs),
            name="polling-watchdog",
        )
        fatal_wait_task = asyncio.create_task(runtime_health.fatal_event.wait(), name="runtime-fatal")
        if notifier.enabled:
            systemd_watchdog_task = asyncio.create_task(
                _systemd_watchdog(notifier, runtime_health),
                name="systemd-watchdog",
            )
        wait_set = {watchdog_task, fatal_wait_task}
        if systemd_watchdog_task:
            wait_set.add(systemd_watchdog_task)
        done, pending = await asyncio.wait(
            wait_set,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if fatal_wait_task in done and runtime_health.fatal_event.is_set():
            raise RuntimeError(runtime_health.fatal_reason or runtime_health.stuck_reason or "runtime fatal error")
        for task in done:
            if task is fatal_wait_task:
                continue
            task.result()
        for task in pending:
            task.cancel()
    finally:
        if fatal_wait_task:
            fatal_wait_task.cancel()
        if systemd_watchdog_task:
            systemd_watchdog_task.cancel()
            await asyncio.gather(systemd_watchdog_task, return_exceptions=True)
        if watchdog_task:
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)
        if notifier.enabled:
            try:
                notifier.stopping("telegram-115-bot stopping")
            except OSError:
                logging.getLogger(__name__).warning("Failed to notify systemd about shutdown")
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
    if user_info.get("_startup_warning"):
        lines.append(f"状态: {user_info['_startup_warning']}")
        return "\n".join(lines)
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


def _make_polling_error_callback(runtime_health: RuntimeHealth):
    def _callback(exc) -> None:
        runtime_health.poll_error_count += 1
        runtime_health.last_poll_error_at = time.monotonic()
        runtime_health.last_error = f"{exc.__class__.__name__}: {exc}"
        logging.getLogger(__name__).warning(
            "Telegram polling error #%s: %s",
            runtime_health.poll_error_count,
            runtime_health.last_error,
        )

    return _callback


async def _polling_watchdog(
    application: Application,
    flow: TaskFlowService,
    chat_id: int,
    runtime_health: RuntimeHealth,
    polling_kwargs: dict,
) -> None:
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(runtime_health.check_interval)
        now = time.monotonic()
        stalled_stage = runtime_health.get_stalled_stage(now)
        if stalled_stage and stalled_stage.fatal:
            stalled_for = int(now - stalled_stage.last_progress_at)
            reason = (
                f"{stalled_stage.label} 已无进度 {stalled_for} 秒，"
                f"超过 {int(stalled_stage.timeout)} 秒"
            )
            runtime_health.mark_fatal(reason)
            raise RuntimeError(reason)
        if runtime_health.get_updates_in_progress:
            elapsed = now - runtime_health.last_get_updates_started_at
            if elapsed > runtime_health.get_updates_stuck_timeout:
                reason = (
                    f"Telegram getUpdates 已卡住 {int(elapsed)} 秒，"
                    f"超过 {runtime_health.get_updates_stuck_timeout} 秒"
                )
                runtime_health.mark_fatal(reason)
                raise RuntimeError(reason)
        try:
            await application.bot.get_me()
        except Exception as exc:
            runtime_health.consecutive_failures += 1
            runtime_health.last_error = f"{exc.__class__.__name__}: {exc}"
            logger.warning(
                "Bot API healthcheck failed (%s/%s): %s",
                runtime_health.consecutive_failures,
                runtime_health.failure_threshold,
                runtime_health.last_error,
            )
            if runtime_health.consecutive_failures < runtime_health.failure_threshold:
                continue
            if not runtime_health.polling_stalled(now):
                poll_age = runtime_health.polling_age(now)
                if poll_age is None:
                    logger.warning(
                        "Skipping updater self-heal restart because polling has not been marked stalled yet"
                    )
                else:
                    logger.warning(
                        "Skipping updater self-heal restart because getUpdates is still within timeout window (%ss < %ss)",
                        int(poll_age),
                        runtime_health.get_updates_stuck_timeout,
                    )
                continue
            if runtime_health.restart_in_progress:
                continue
            runtime_health.restart_in_progress = True
            try:
                await _restart_updater(application, polling_kwargs)
                await application.bot.get_me()
            except Exception as restart_exc:
                runtime_health.restart_failures += 1
                logger.exception(
                    "Updater self-heal failed (%s/%s)",
                    runtime_health.restart_failures,
                    runtime_health.max_restart_failures,
                )
                if runtime_health.restart_failures >= runtime_health.max_restart_failures:
                    raise RuntimeError(
                        "Telegram polling could not recover after repeated restart attempts"
                    ) from restart_exc
            else:
                recovered_after = runtime_health.consecutive_failures
                logger.warning("Updater polling recovered after self-heal restart")
                runtime_health.consecutive_failures = 0
                runtime_health.restart_failures = 0
                runtime_health.last_api_ok_at = time.monotonic()
                runtime_health.clear_stuck()
                runtime_health.mark_progress("updater self-heal ok")
                await _notify_watchdog_event(
                    flow,
                    chat_id,
                    f"Bot 轮询已自动恢复，已重启 updater。\n恢复前连续失败次数：{recovered_after}",
                )
            finally:
                runtime_health.restart_in_progress = False
        else:
            if runtime_health.consecutive_failures:
                recovered_after = runtime_health.consecutive_failures
                logger.info("Bot API healthcheck recovered after %s failure(s)", recovered_after)
            runtime_health.consecutive_failures = 0
            runtime_health.restart_failures = 0
            runtime_health.last_api_ok_at = time.monotonic()
            runtime_health.mark_progress("bot api healthcheck ok")


async def _restart_updater(application: Application, polling_kwargs: dict) -> None:
    async with asyncio.timeout(60):
        if application.updater.running:
            await application.updater.stop()
        await application.updater.start_polling(**polling_kwargs)


async def _notify_watchdog_event(flow: TaskFlowService, chat_id: int, text: str) -> None:
    try:
        await flow.notify(chat_id, text)
    except Exception:
        logging.getLogger(__name__).exception("Failed to send watchdog notification")


async def _startup_probe(
    application: Application,
    aria2: Aria2RPCService,
    runtime_health: RuntimeHealth,
) -> None:
    await application.bot.get_me()
    runtime_health.last_api_ok_at = time.monotonic()
    runtime_health.mark_progress("bot api bootstrap ok")
    if aria2.settings.enable:
        async with asyncio.timeout(runtime_health.aria2_rpc_timeout):
            await asyncio.to_thread(aria2.get_version, runtime_health.aria2_rpc_timeout)
        runtime_health.mark_progress("aria2 bootstrap ok")


async def _load_startup_user_info(
    open115: Open115Client,
    runtime_health: RuntimeHealth,
) -> dict[str, object]:
    logger = logging.getLogger(__name__)
    for attempt in range(5):
        try:
            user_info = await asyncio.to_thread(open115.get_user_info)
        except Open115TemporaryError as exc:
            delay = min(5 * (attempt + 1), 30)
            logger.warning("115 user info bootstrap failed temporarily (%s/5): %s", attempt + 1, exc)
            if attempt == 4:
                break
            await asyncio.sleep(delay)
        except Exception as exc:
            logger.warning("115 user info bootstrap skipped: %s", exc)
            return {"_startup_warning": f"启动时获取 115 用户信息失败，已降级启动。原因: {exc}"}
        else:
            runtime_health.mark_progress("115 bootstrap ok")
            return user_info
    warning = "启动时获取 115 用户信息失败，115 接口暂时不可用，已降级启动。"
    runtime_health.last_error = warning
    return {"_startup_warning": warning}


async def _systemd_watchdog(notifier: SystemdNotifier, runtime_health: RuntimeHealth) -> None:
    interval = notifier.watchdog_interval(default=30)
    while True:
        await asyncio.sleep(interval)
        if runtime_health.fatal_event.is_set() or runtime_health.stuck_reason:
            continue
        notifier.watchdog(_systemd_status(runtime_health))


def _log_proxy_summary(settings: Settings) -> None:
    logging.getLogger(__name__).info(
        "Proxy summary: HTTP=%s HTTPS=%s NO_PROXY=%s",
        _sanitize_proxy(settings.proxy.http),
        _sanitize_proxy(settings.proxy.https),
        settings.proxy.no_proxy or "-",
    )


def _sanitize_proxy(raw_proxy: str) -> str:
    if not raw_proxy:
        return "-"
    normalized = raw_proxy if "://" in raw_proxy else f"http://{raw_proxy}"
    parsed = urlparse(normalized)
    if not parsed.hostname:
        return raw_proxy
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _systemd_status(runtime_health: RuntimeHealth) -> str:
    last_error = runtime_health.last_error or "ok"
    last_activity = runtime_health.last_activity or "-"
    return (
        "progress="
        f"{int(time.monotonic() - runtime_health.last_progress_at)}s "
        f"activity={last_activity} "
        f"last_error={last_error}"
    )


if __name__ == "__main__":
    asyncio.run(main())
