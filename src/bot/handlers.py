from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import BotCommand, Message, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import Settings
from src.services.open115 import Open115Client
from src.services.task_flow import MessageRef, TaskFlowService


HELP_TEXT = """可用命令：
/start - 查看帮助
/auth - 115 二维码授权
/av <番号> - 搜索磁力并自动下载
/q - 取消当前目录选择

直接发送 magnet 链接会进入 115 离线下载流程。
直接发送 video/document 会进入上传到 115 的流程。"""


logger = logging.getLogger(__name__)


def register_handlers(application: Application, settings: Settings, flow: TaskFlowService, open115: Open115Client) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("auth", auth))
    application.add_handler(CommandHandler("av", av))
    application.add_handler(CommandHandler("q", cancel))
    application.add_handler(CallbackQueryHandler(selection_callback, pattern=r"^(selm|sell|sellast|selc):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, media_message))

    application.bot_data["settings"] = settings
    application.bot_data["flow"] = flow
    application.bot_data["open115"] = open115
    application.add_error_handler(on_error)


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "查看帮助"),
        BotCommand("auth", "115 授权"),
        BotCommand("av", "番号搜索并下载"),
        BotCommand("q", "取消当前选择"),
    ]
    await application.bot.set_my_commands(commands)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_allowed(update, context):
        return
    await _reply_text_with_retry(update.effective_message, HELP_TEXT)


async def auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_allowed(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    open115: Open115Client = context.application.bot_data["open115"]
    flow: TaskFlowService = context.application.bot_data["flow"]

    qr_path = Path(settings.upload.temp_dir) / f"115-auth-{update.effective_chat.id}.png"
    try:
        auth_session = await asyncio.to_thread(open115.create_auth_session, qr_path)
    except Exception as exc:
        await _reply_text_with_retry(update.effective_message, f"无法创建 115 授权二维码: {exc}")
        return

    with auth_session.qr_path.open("rb") as handle:
        await _reply_photo_with_retry(
            update.effective_message,
            photo=handle,
            caption="请使用 115 App 扫码授权，机器人会在授权成功后通知你。",
        )
    asyncio.create_task(flow.run_auth(update.effective_chat.id, auth_session))


async def av(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_allowed(update, context):
        return
    flow: TaskFlowService = context.application.bot_data["flow"]
    if not context.args:
        await _reply_text_with_retry(update.effective_message, "请使用 /av <番号>。")
        return
    query = " ".join(context.args).strip()
    selection = flow.create_selection(
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id,
        kind="av",
        payload={"query": query},
    )
    await _reply_text_with_retry(
        update.effective_message,
        f"请为 {query} 选择 115 保存目录。",
        reply_markup=flow.build_main_keyboard(selection),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_allowed(update, context):
        return
    flow: TaskFlowService = context.application.bot_data["flow"]
    flow.clear_chat_pending(update.effective_chat.id)
    await _reply_text_with_retry(update.effective_message, "已取消当前目录选择。")


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_allowed(update, context):
        return
    text = (update.effective_message.text or "").strip()
    flow: TaskFlowService = context.application.bot_data["flow"]
    if not flow.is_magnet(text):
        return
    selection = flow.create_selection(
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id,
        kind="magnet",
        payload={"magnet": text},
    )
    await _reply_text_with_retry(
        update.effective_message,
        "请选择 115 保存目录。",
        reply_markup=flow.build_main_keyboard(selection),
    )


async def media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_allowed(update, context):
        return
    message = update.effective_message
    file_name = None
    file_id = None
    if message.video:
        file_name = message.video.file_name or f"{message.video.file_unique_id}.mp4"
        file_id = message.video.file_id
    elif message.document:
        file_name = message.document.file_name or f"{message.document.file_unique_id}.bin"
        file_id = message.document.file_id
    if not file_name or not file_id:
        return

    flow: TaskFlowService = context.application.bot_data["flow"]
    selection = flow.create_selection(
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id,
        kind="upload",
        payload={
            "message_ref": MessageRef(
                chat_id=update.effective_chat.id,
                user_id=update.effective_user.id,
                message_id=message.message_id,
                file_name=file_name,
                file_id=file_id,
            )
        },
    )
    await _reply_text_with_retry(
        update.effective_message,
        f"请选择 {file_name} 要保存到 115 的目录。",
        reply_markup=flow.build_main_keyboard(selection),
    )


async def selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    flow: TaskFlowService = context.application.bot_data["flow"]
    payload = query.data.split(":")
    action = payload[0]
    selection_id = payload[1]
    selection = flow.get_selection(selection_id)

    if action == "selc":
        if selection:
            flow.pop_selection(selection_id)
        await query.edit_message_text("已取消。")
        return

    if not selection:
        await query.edit_message_text("该选择已过期，请重新发起命令。")
        return

    if action == "selm":
        category_name = payload[2]
        try:
            reply_markup = flow.build_sub_keyboard(selection, category_name)
        except KeyError:
            await query.edit_message_text("目录分类不存在。")
            return
        await query.edit_message_text("请选择具体目录。", reply_markup=reply_markup)
        return

    if action == "sellast":
        save_path = flow.last_save_path.get(selection.chat_id)
        if not save_path:
            await query.edit_message_text("没有可用的上次保存目录，请重新选择。")
            return
        await _dispatch_selection(query, flow, selection, save_path)
        return

    if action == "sell":
        option_id = payload[2]
        try:
            save_path = flow.resolve_save_path(selection, option_id)
        except KeyError:
            await query.edit_message_text("目录选择已失效，请重新发起。")
            return
        await _dispatch_selection(query, flow, selection, save_path)


async def _dispatch_selection(query, flow: TaskFlowService, selection, save_path: str) -> None:
    flow.last_save_path[selection.chat_id] = save_path
    flow.pop_selection(selection.selection_id)
    if selection.kind == "magnet":
        magnet = selection.payload["magnet"]
        await query.edit_message_text(f"任务已提交。\n保存目录: {save_path}")
        await flow.start_magnet_task(
            chat_id=selection.chat_id,
            user_id=selection.user_id,
            magnet=magnet,
            save_path=save_path,
            label=magnet[:64],
        )
        return
    if selection.kind == "av":
        query_text = selection.payload["query"]
        await query.edit_message_text(f"/av 任务已提交。\n保存目录: {save_path}")
        await flow.start_av_task(
            chat_id=selection.chat_id,
            user_id=selection.user_id,
            query=query_text,
            save_path=save_path,
        )
        return
    if selection.kind == "upload":
        message_ref: MessageRef = selection.payload["message_ref"]
        await query.edit_message_text(f"上传任务已提交。\n保存目录: {save_path}")
        await flow.start_upload_task(ref=message_ref, save_path=save_path)


async def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    if user and settings.is_allowed(user.id):
        return True
    if update.effective_message:
        await _reply_text_with_retry(update.effective_message, "你没有权限使用这个 bot。")
    return False


async def _reply_text_with_retry(message: Message | None, text: str, **kwargs) -> None:
    if not message:
        return
    for attempt in range(3):
        try:
            await message.reply_text(text, **kwargs)
            return
        except (TimedOut, NetworkError):
            if attempt == 2:
                raise
            await asyncio.sleep(1 + attempt)


async def _reply_photo_with_retry(message: Message | None, **kwargs) -> None:
    if not message:
        return
    for attempt in range(3):
        try:
            await message.reply_photo(**kwargs)
            return
        except (TimedOut, NetworkError):
            if attempt == 2:
                raise
            await asyncio.sleep(1 + attempt)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, TimedOut):
        logger.warning("Telegram Bot API timed out while processing update")
        return
    logger.error(
        "Unhandled telegram update error",
        exc_info=(type(context.error), context.error, context.error.__traceback__),
    )
