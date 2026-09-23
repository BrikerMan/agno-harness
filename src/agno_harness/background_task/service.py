"""Create a task row, run it on the asyncio loop, and post the result back."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.channel import OutboundMessage
from ..sessions.models import ConversationKey
from .context import current_origin
from .handlers import BackgroundHandler
from .store import BackgroundTaskStore, TaskRecord

log = logging.getLogger("agno_harness.background_task")


class BackgroundTaskService:
    """Sqlite-backed tasks for one process. The row survives; the run does not."""

    def __init__(self, db_path: str) -> None:
        self.store = BackgroundTaskStore(db_path)
        self._handlers: dict[str, BackgroundHandler] = {}
        self._relay: Any = None
        self._inflight: set[asyncio.Task[None]] = set()

    def register(self, handler: BackgroundHandler) -> None:
        self._handlers[handler.task_type] = handler

    def bind(self, relay: Any) -> None:
        """Relay used to deliver the finished result to the originating chat."""
        self._relay = relay

    def catalog(self) -> str:
        if not self._handlers:
            return "No background task types are registered."
        lines = ["Background task types:"]
        for handler in self._handlers.values():
            lines.append(f"- {handler.task_type}: {handler.description}")
        return "\n".join(lines)

    async def list_running(self) -> list[TaskRecord]:
        return await self.store.list_active()

    async def get(self, task_id: int) -> TaskRecord | None:
        return await self.store.get(task_id)

    async def create(
        self,
        *,
        task_type: str,
        subject: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[TaskRecord | None, str]:
        handler = self._handlers.get(task_type)
        if handler is None:
            known = ", ".join(sorted(self._handlers)) or "none"
            return None, f"Unknown task type {task_type!r}. Known types: {known}."
        record = await self.store.insert(
            task_type=task_type,
            subject=subject,
            params=params or {},
            origin=current_origin(),
        )
        running = asyncio.create_task(self._run(record.id))
        self._inflight.add(running)
        running.add_done_callback(self._inflight.discard)
        return record, (
            f"Created background task #{record.id} ({task_type}, {subject}). "
            "It is running in the background. The result will be posted back to this chat."
        )

    async def _run(self, task_id: int) -> None:
        task = await self.store.get(task_id)
        if task is None:
            return
        handler = self._handlers.get(task.task_type)
        if handler is None:
            await self.store.mark_failed(task_id, f"No handler for {task.task_type}.")
            return
        await self.store.mark_running(task_id)
        try:
            result = await handler.run(task)
        except Exception as exc:
            log.exception("background task #%s failed", task_id)
            await self.store.mark_failed(task_id, str(exc))
            await self._notify(task_id, ok=False, summary=str(exc))
            return
        await self.store.mark_done(task_id, result)
        await self._notify(task_id, ok=True, summary=result)

    async def _notify(self, task_id: int, *, ok: bool, summary: str) -> None:
        task = await self.store.get(task_id)
        if task is None or self._relay is None or not task.origin:
            return
        platform = str(task.origin.get("platform") or "")
        key_data = task.origin.get("key")
        channels = getattr(self._relay, "channels", {})
        if platform not in channels or not isinstance(key_data, dict):
            log.warning("background task #%s has no chat to notify", task_id)
            return
        channel = channels[platform][0]
        key = ConversationKey.model_validate(key_data)
        status = "finished" if ok else "failed"
        text = f"Background task #{task.id} ({task.task_type}) {status}.\n\n{summary}"
        try:
            await channel.send(key, OutboundMessage(text=text))
        except Exception:
            log.exception("background task #%s could not post the result", task_id)
