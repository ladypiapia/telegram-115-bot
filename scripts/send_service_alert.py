#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import socket
from datetime import datetime

import requests

from src.config import load_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send Telegram alerts for service events.")
    parser.add_argument(
        "event",
        nargs="?",
        default="unexpected-exit",
        help="Event name shown in the notification.",
    )
    parser.add_argument(
        "--service",
        default="telegram-115-bot",
        help="Service name shown in the notification.",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("BOT_CONFIG"),
        help="Optional path to the bot config file.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Always send the notification even when systemd reports a clean stop.",
    )
    return parser.parse_args()


def should_notify(force: bool) -> bool:
    if force:
        return True
    service_result = os.getenv("SERVICE_RESULT", "").strip().lower()
    if not service_result:
        return True
    return service_result not in {"success", "clean-exit"}


def build_message(service_name: str, event: str) -> str:
    hostname = socket.gethostname()
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    service_result = os.getenv("SERVICE_RESULT", "unknown")
    exit_code = os.getenv("EXIT_CODE", "unknown")
    exit_status = os.getenv("EXIT_STATUS", "unknown")
    lines = [
        "机器人进程异常退出，systemd 将按策略自动重启。",
        f"服务: {service_name}",
        f"事件: {event}",
        f"机器: {hostname}",
        f"时间: {now}",
        f"SERVICE_RESULT: {service_result}",
        f"EXIT_CODE: {exit_code}",
        f"EXIT_STATUS: {exit_status}",
    ]
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if not should_notify(args.force):
        return 0

    settings = load_settings(args.config)
    settings.apply_proxy_env()

    response = requests.post(
        f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
        data={
            "chat_id": settings.allowed_user,
            "text": build_message(args.service, args.event),
        },
        timeout=20,
    )
    response.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
