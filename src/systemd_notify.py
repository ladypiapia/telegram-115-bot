from __future__ import annotations

import os
import socket


class SystemdNotifier:
    def __init__(self) -> None:
        self.socket_path = os.getenv("NOTIFY_SOCKET", "")
        self.watchdog_usec = int(os.getenv("WATCHDOG_USEC", "0") or "0")

    @property
    def enabled(self) -> bool:
        return bool(self.socket_path)

    def watchdog_interval(self, default: int = 30) -> int:
        if self.watchdog_usec <= 0:
            return default
        seconds = max(int(self.watchdog_usec / 1_000_000 / 2), 1)
        return seconds

    def notify(self, *lines: str) -> None:
        if not self.enabled:
            return
        address = self.socket_path
        if address.startswith("@"):
            address = "\0" + address[1:]
        payload = "\n".join(line for line in lines if line).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(payload)

    def ready(self, status: str = "") -> None:
        self.notify("READY=1", f"STATUS={status}" if status else "")

    def watchdog(self, status: str = "") -> None:
        self.notify("WATCHDOG=1", f"STATUS={status}" if status else "")

    def stopping(self, status: str = "") -> None:
        self.notify("STOPPING=1", f"STATUS={status}" if status else "")
