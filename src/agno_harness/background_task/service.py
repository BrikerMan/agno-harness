"""Create a task row, run it on the asyncio loop, and post the result back."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.channel import OutboundMessage
from ..db.classes import AgnoHarnessDb, AgnoHarnessPostgresDb, AgnoHarnessSqliteDb
from ..sessions.models import ConversationKey
from .context import current_origin
from .handlers import BackgroundHandler
from .store import BackgroundTaskStore, TaskRecord

log = logging.getLogger("agno_harness.background_task")

#: Channels whose ``send`` renders cards; others get the message text.
CARD_PLATFORMS = frozenset({"teams", "lark"})
#: Runs per task, counting the first, unless the handler sets ``max_attempts``.
MAX_ATTEMPTS = 2
#: Seconds before a failed resumable task runs again, times the attempts so far.
RETRY_DELAY = 30.0
INTERRUPTED = "Interrupted by a restart."


def _max_attempts(handler: BackgroundHandler | None) -> int:
    return int(getattr(handler, "max_attempts", MAX_ATTEMPTS))


def _can_retry(handler: BackgroundHandler | None, task: TaskRecord) -> bool:
    return bool(getattr(handler, "resumable", False)) and task.attempts < _max_attempts(handler)


def _open_harness(target: str) -> AgnoHarnessDb:
    """A filesystem path is a SQLite file. A URL is SQLite or Postgres."""
    if "://" not in target:
        return AgnoHarnessSqliteDb(db_file=target)
    if target.startswith("sqlite"):
        return AgnoHarnessSqliteDb(db_url=target)
    return AgnoHarnessPostgresDb(db_url=target)


class BackgroundTaskService:
    """Tasks for one process. Rows live on the harness background-task table.

    ``db`` is a filesystem path, a database URL, or an ``AgnoHarnessDb``.
    Rows go to ``agno_harness_background_tasks``, not to an app-prefixed table.
    The row survives a restart; ``resume`` decides the run.
    """

    def __init__(self, db: str | AgnoHarnessDb | None = None) -> None:
        self._harness: AgnoHarnessDb | None = None
        self.store: BackgroundTaskStore | None = None
        self._handlers: dict[str, BackgroundHandler] = {}
        self._relay: Any = None
        self._inflight: set[asyncio.Task[None]] = set()
        if db is not None:
            self.attach(db)

    def attach(self, db: str | AgnoHarnessDb) -> None:
        """Use this database connection. Task rows stay in ``agno_harness_background_tasks``."""
        self._harness = db if isinstance(db, AgnoHarnessDb) else _open_harness(db)
        self.store = BackgroundTaskStore(self._harness)

    def _store(self) -> BackgroundTaskStore:
        if self.store is None:
            raise RuntimeError("BackgroundTaskService has no database. Call attach() first.")
        return self.store

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
        return await self._store().list_active()

    async def get(self, task_id: int) -> TaskRecord | None:
        return await self._store().get(task_id)

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
        record = await self._store().insert(
            task_type=task_type,
            subject=subject,
            params=params or {},
            origin=current_origin(),
        )
        self._spawn(record.id)
        return record, (
            f"Created background task #{record.id} ({task_type}, {subject}). "
            "It is running in the background. The result will be posted back to this chat."
        )

    async def resume(self) -> list[int]:
        """Call once at startup, after ``bind``. Tasks a restart left unfinished run again when their
        handler is resumable and attempts remain; the rest are failed and their chat is told.
        Returns the ids that run again."""
        restarted = []
        for task in await self._store().list_active():
            handler = self._handlers.get(task.task_type)
            if handler is not None and _can_retry(handler, task):
                log.info("resuming background task #%s (attempt %s)", task.id, task.attempts + 1)
                self._spawn(task.id)
                restarted.append(task.id)
                continue
            await self._store().mark_failed(task.id, INTERRUPTED)
            await self._notify(
                task.id, await self._failure_message(handler, task, INTERRUPTED), ok=False
            )
        return restarted

    def _spawn(self, task_id: int) -> None:
        running = asyncio.create_task(self._run(task_id))
        self._inflight.add(running)
        running.add_done_callback(self._inflight.discard)

    async def _run(self, task_id: int) -> None:
        task = await self._store().get(task_id)
        if task is None:
            return
        handler = self._handlers.get(task.task_type)
        if handler is None:
            await self._store().mark_failed(task_id, f"No handler for {task.task_type}.")
            return
        while True:
            await self._store().mark_running(task_id)
            task = await self._store().get(task_id) or task
            try:
                result = await handler.run(task)
                break
            except Exception as exc:
                if _can_retry(handler, task):
                    delay = getattr(handler, "retry_delay", RETRY_DELAY) * task.attempts
                    log.exception(
                        "background task #%s failed (attempt %s of %s), retrying in %ss",
                        task_id,
                        task.attempts,
                        _max_attempts(handler),
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                log.exception("background task #%s failed", task_id)
                await self._store().mark_failed(task_id, str(exc))
                await self._notify(
                    task_id, await self._failure_message(handler, task, str(exc)), ok=False
                )
                return
        message = result if isinstance(result, OutboundMessage) else OutboundMessage(text=result)
        await self._store().mark_done(task_id, message.text or message.raw_text)
        await self._notify(task_id, message, ok=True)

    async def _failure_message(
        self, handler: BackgroundHandler | None, task: TaskRecord, error: str
    ) -> OutboundMessage:
        hook = getattr(handler, "on_failure", None)
        if hook is not None:
            try:
                custom = await hook(task, error)
            except Exception:
                log.exception("background task #%s on_failure hook failed", task.id)
                custom = None
            if isinstance(custom, OutboundMessage):
                return custom
            if isinstance(custom, str):
                return OutboundMessage(text=custom)
        return OutboundMessage(text=error)

    async def _notify(self, task_id: int, message: OutboundMessage, *, ok: bool) -> None:
        task = await self._store().get(task_id)
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
        if message.cards and platform in CARD_PLATFORMS:
            outbound = OutboundMessage(cards=message.cards, extra=message.extra)
        elif message.cards:
            outbound = OutboundMessage(text=message.text or message.raw_text, extra=message.extra)
        else:
            status = "finished" if ok else "failed"
            text = f"Background task #{task.id} ({task.task_type}) {status}.\n\n{message.text}"
            outbound = OutboundMessage(text=text, extra=message.extra)
        try:
            await channel.send(key, outbound)
        except Exception:
            log.exception("background task #%s could not post the result", task_id)
