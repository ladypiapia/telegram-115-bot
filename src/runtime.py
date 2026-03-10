from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


class RuntimeFatalError(RuntimeError):
    """Raised when the process should exit and let systemd restart it."""


@dataclass(slots=True)
class ActiveStage:
    key: str
    label: str
    timeout: float
    fatal: bool
    started_at: float
    last_progress_at: float


@dataclass(slots=True)
class RuntimeHealth:
    check_interval: int
    failure_threshold: int
    max_restart_failures: int
    get_updates_stuck_timeout: int
    handler_timeout: int
    blocking_stage_timeout: int
    aria2_rpc_timeout: int
    telegram_transfer_timeout: int
    telethon_stall_timeout: int
    consecutive_failures: int = 0
    restart_failures: int = 0
    poll_error_count: int = 0
    last_api_ok_at: float = 0.0
    last_poll_error_at: float = 0.0
    last_get_updates_started_at: float = 0.0
    last_get_updates_finished_at: float = 0.0
    last_update_started_at: float = 0.0
    last_update_finished_at: float = 0.0
    last_progress_at: float = 0.0
    last_activity: str = ""
    last_error: str = ""
    stuck_reason: str = ""
    current_update_name: str = ""
    restart_in_progress: bool = False
    get_updates_in_progress: bool = False
    fatal_reason: str = ""
    active_stages: dict[str, ActiveStage] = field(default_factory=dict)
    fatal_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def mark_progress(self, label: str | None = None) -> None:
        now = time.monotonic()
        self.last_progress_at = now
        if label:
            self.last_activity = label

    def mark_get_updates_start(self) -> None:
        self.last_get_updates_started_at = time.monotonic()
        self.get_updates_in_progress = True

    def mark_get_updates_end(self) -> None:
        now = time.monotonic()
        self.last_get_updates_finished_at = now
        self.get_updates_in_progress = False
        self.mark_progress("get_updates ok")

    def mark_update_start(self, name: str) -> None:
        now = time.monotonic()
        self.last_update_started_at = now
        self.current_update_name = name
        self.mark_progress(f"update start: {name}")

    def mark_update_end(self) -> None:
        self.last_update_finished_at = time.monotonic()
        self.current_update_name = ""
        self.mark_progress("update done")

    def start_stage(self, key: str, label: str, timeout: float, fatal: bool = True) -> None:
        now = time.monotonic()
        self.active_stages[key] = ActiveStage(
            key=key,
            label=label,
            timeout=timeout,
            fatal=fatal,
            started_at=now,
            last_progress_at=now,
        )
        self.mark_progress(label)

    def touch_stage(self, key: str) -> None:
        stage = self.active_stages.get(key)
        if not stage:
            return
        now = time.monotonic()
        stage.last_progress_at = now
        self.last_progress_at = now

    def finish_stage(self, key: str) -> None:
        stage = self.active_stages.pop(key, None)
        if stage:
            self.mark_progress(f"stage done: {stage.label}")

    def get_stalled_stage(self, now: float | None = None) -> ActiveStage | None:
        current = now or time.monotonic()
        for stage in self.active_stages.values():
            if current - stage.last_progress_at > stage.timeout:
                return stage
        return None

    def polling_stalled(self, now: float | None = None) -> bool:
        current = now or time.monotonic()
        if self.get_updates_in_progress:
            return current - self.last_get_updates_started_at > self.get_updates_stuck_timeout
        if self.last_get_updates_finished_at <= 0:
            return False
        return current - self.last_get_updates_finished_at > self.get_updates_stuck_timeout

    def polling_age(self, now: float | None = None) -> float | None:
        current = now or time.monotonic()
        if self.get_updates_in_progress and self.last_get_updates_started_at > 0:
            return current - self.last_get_updates_started_at
        if self.last_get_updates_finished_at > 0:
            return current - self.last_get_updates_finished_at
        return None

    def mark_fatal(self, reason: str) -> None:
        self.fatal_reason = self.fatal_reason or reason
        self.stuck_reason = reason
        self.last_error = reason
        self.fatal_event.set()

    def clear_stuck(self) -> None:
        if not self.fatal_event.is_set():
            self.stuck_reason = ""
