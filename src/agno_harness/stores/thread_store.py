from __future__ import annotations

import abc
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update

from .mixins import load_json, store_json
from .sql_models import get_or_create_thread_model


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return float(value.timestamp())
    except Exception:
        return None


def thread_row_to_dict(row: Any) -> dict[str, Any]:
    """Serialize a thread model row or dict into a standard UI-friendly summary."""
    if isinstance(row, dict):
        return row

    created_at = _epoch(getattr(row, "created_at", None))
    last_active_at = _epoch(getattr(row, "last_active_at", None))
    last_finished_at = _epoch(getattr(row, "last_finished_at", None))

    return {
        "threadId": getattr(row, "thread_id", ""),
        "userId": getattr(row, "user_id", None),
        "title": getattr(row, "title", "New Chat"),
        "status": getattr(row, "status", "running"),
        "isPaused": bool(getattr(row, "is_paused", False)),
        "isError": bool(getattr(row, "is_error", False)),
        "errorReason": getattr(row, "error_reason", None),
        "runCount": int(getattr(row, "run_count", 0)),
        "lastRunId": getattr(row, "last_run_id", None),
        "isDeleted": bool(getattr(row, "is_deleted", False)),
        "createdAt": created_at,
        "updatedAt": last_active_at or created_at,
        "lastActiveAt": last_active_at or created_at,
        "lastFinishedAt": last_finished_at,
        "messageCount": 0,
        "metadata": load_json(getattr(row, "metadata_json", None)) or {},
    }


class BaseThreadStore(abc.ABC):
    """Abstract interface for storing and managing first-class Thread records."""

    @abc.abstractmethod
    async def start_turn(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the start of a conversation turn in the thread."""
        ...

    @abc.abstractmethod
    async def set_paused(
        self,
        thread_id: str,
        *,
        is_paused: bool = True,
        run_id: str | None = None,
    ) -> None:
        """Mark thread as waiting for HITL or user feedback."""
        ...

    @abc.abstractmethod
    async def set_finished(
        self,
        thread_id: str,
        *,
        is_error: bool = False,
        error_reason: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Mark thread turn as finished or errored."""
        ...

    @abc.abstractmethod
    async def set_cancelled(
        self,
        thread_id: str,
        *,
        reason: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Mark thread turn as cancelled/aborted."""
        ...

    @abc.abstractmethod
    async def set_title(self, thread_id: str, title: str) -> None:
        """Update the thread's display title."""
        ...

    @abc.abstractmethod
    async def get_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Retrieve a thread summary by ID."""
        ...

    @abc.abstractmethod
    async def list_threads(
        self,
        *,
        user_id: str | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List threads sorted newest first."""
        ...

    @abc.abstractmethod
    async def delete_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        hard: bool = False,
    ) -> bool:
        """Delete thread (soft delete by default, hard purge if requested)."""
        ...


class InMemoryThreadStore(BaseThreadStore):
    """In-memory thread store for testing and ephemeral execution."""

    def __init__(self) -> None:
        self._threads: dict[str, dict[str, Any]] = {}

    async def start_turn(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).timestamp()
        existing = self._threads.get(thread_id)
        if existing is None:
            record: dict[str, Any] = {
                "threadId": thread_id,
                "userId": user_id,
                "title": title or "New Chat",
                "status": "running",
                "isPaused": False,
                "isError": False,
                "errorReason": None,
                "runCount": 1,
                "lastRunId": run_id,
                "isDeleted": False,
                "createdAt": now,
                "updatedAt": now,
                "lastActiveAt": now,
                "lastFinishedAt": None,
                "messageCount": 0,
                "metadata": metadata or {},
            }
            self._threads[thread_id] = record
            return record

        existing["status"] = "running"
        existing["isPaused"] = False
        existing["isError"] = False
        existing["errorReason"] = None
        existing["runCount"] = existing.get("runCount", 0) + 1
        existing["lastActiveAt"] = now
        existing["updatedAt"] = now
        existing["isDeleted"] = False
        if run_id:
            existing["lastRunId"] = run_id
        if user_id and not existing.get("userId"):
            existing["userId"] = user_id
        if title and existing.get("title") == "New Chat":
            existing["title"] = title
        if metadata:
            existing.setdefault("metadata", {}).update(metadata)
        return existing

    async def set_paused(
        self,
        thread_id: str,
        *,
        is_paused: bool = True,
        run_id: str | None = None,
    ) -> None:
        record = self._threads.get(thread_id)
        if record is not None:
            now = datetime.now(UTC).timestamp()
            record["status"] = "paused" if is_paused else "running"
            record["isPaused"] = is_paused
            record["lastActiveAt"] = now
            record["updatedAt"] = now
            if run_id:
                record["lastRunId"] = run_id

    async def set_finished(
        self,
        thread_id: str,
        *,
        is_error: bool = False,
        error_reason: str | None = None,
        run_id: str | None = None,
    ) -> None:
        record = self._threads.get(thread_id)
        if record is not None:
            now = datetime.now(UTC).timestamp()
            record["status"] = "error" if is_error else "finished"
            record["isPaused"] = False
            record["isError"] = is_error
            record["errorReason"] = error_reason
            record["lastFinishedAt"] = now
            record["lastActiveAt"] = now
            record["updatedAt"] = now
            if run_id:
                record["lastRunId"] = run_id

    async def set_cancelled(
        self,
        thread_id: str,
        *,
        reason: str | None = None,
        run_id: str | None = None,
    ) -> None:
        record = self._threads.get(thread_id)
        if record is not None:
            now = datetime.now(UTC).timestamp()
            record["status"] = "cancelled"
            record["isPaused"] = False
            record["errorReason"] = reason
            record["lastFinishedAt"] = now
            record["lastActiveAt"] = now
            record["updatedAt"] = now
            if run_id:
                record["lastRunId"] = run_id

    async def set_title(self, thread_id: str, title: str) -> None:
        record = self._threads.get(thread_id)
        if record is not None:
            record["title"] = title

    async def get_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        record = self._threads.get(thread_id)
        if record is None:
            return None
        if not include_deleted and record.get("isDeleted"):
            return None
        if (
            user_id is not None
            and record.get("userId") is not None
            and record.get("userId") != user_id
        ):
            return None
        return dict(record)

    async def list_threads(
        self,
        *,
        user_id: str | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        results = [
            dict(r)
            for r in self._threads.values()
            if (include_deleted or not r.get("isDeleted"))
            and (user_id is None or r.get("userId") is None or r.get("userId") == user_id)
        ]
        results.sort(key=lambda t: t.get("lastActiveAt") or 0.0, reverse=True)
        if offset:
            results = results[offset:]
        if limit is not None:
            results = results[:limit]
        return results

    async def delete_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        hard: bool = False,
    ) -> bool:
        record = self._threads.get(thread_id)
        if record is None:
            return False
        if (
            user_id is not None
            and record.get("userId") is not None
            and record.get("userId") != user_id
        ):
            return False

        if hard:
            del self._threads[thread_id]
        else:
            record["isDeleted"] = True
            record["deletedAt"] = datetime.now(UTC).timestamp()
        return True


class SQLAlchemyThreadStore(BaseThreadStore):
    """SQLAlchemy persistent implementation of BaseThreadStore."""

    def __init__(
        self,
        session_factory: Any,
        model: type[Any] | None = None,
        *,
        table_name: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.model = model or get_or_create_thread_model(table_name)

    async def start_turn(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.session_factory() as session, session.begin():
            stmt = select(self.model).where(self.model.thread_id == thread_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            now = datetime.now(UTC)

            if row is None:
                row = self.model(
                    thread_id=thread_id,
                    user_id=user_id,
                    title=title or "New Chat",
                    status="running",
                    is_paused=False,
                    is_error=False,
                    error_reason=None,
                    run_count=1,
                    last_run_id=run_id,
                    is_deleted=False,
                    created_at=now,
                    last_active_at=now,
                    last_finished_at=None,
                    metadata_json=store_json(metadata) if metadata else None,
                )
                session.add(row)
                await session.flush()
                return thread_row_to_dict(row)

            row.status = "running"
            row.is_paused = False
            row.is_error = False
            row.error_reason = None
            row.run_count = (row.run_count or 0) + 1
            row.last_active_at = now
            row.is_deleted = False
            row.deleted_at = None
            if run_id:
                row.last_run_id = run_id
            if user_id and not row.user_id:
                row.user_id = user_id
            if title and (not row.title or row.title == "New Chat"):
                row.title = title
            if metadata:
                existing_meta = load_json(row.metadata_json) or {}
                row.metadata_json = store_json({**existing_meta, **metadata})

            await session.flush()
            return thread_row_to_dict(row)

    async def set_paused(
        self,
        thread_id: str,
        *,
        is_paused: bool = True,
        run_id: str | None = None,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            values: dict[str, Any] = {
                "status": "paused" if is_paused else "running",
                "is_paused": is_paused,
                "last_active_at": datetime.now(UTC),
            }
            if run_id:
                values["last_run_id"] = run_id
            stmt = update(self.model).where(self.model.thread_id == thread_id).values(**values)
            await session.execute(stmt)

    async def set_finished(
        self,
        thread_id: str,
        *,
        is_error: bool = False,
        error_reason: str | None = None,
        run_id: str | None = None,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            now = datetime.now(UTC)
            values: dict[str, Any] = {
                "status": "error" if is_error else "finished",
                "is_paused": False,
                "is_error": is_error,
                "error_reason": error_reason,
                "last_finished_at": now,
                "last_active_at": now,
            }
            if run_id:
                values["last_run_id"] = run_id
            stmt = update(self.model).where(self.model.thread_id == thread_id).values(**values)
            await session.execute(stmt)

    async def set_cancelled(
        self,
        thread_id: str,
        *,
        reason: str | None = None,
        run_id: str | None = None,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            now = datetime.now(UTC)
            values: dict[str, Any] = {
                "status": "cancelled",
                "is_paused": False,
                "error_reason": reason,
                "last_finished_at": now,
                "last_active_at": now,
            }
            if run_id:
                values["last_run_id"] = run_id
            stmt = update(self.model).where(self.model.thread_id == thread_id).values(**values)
            await session.execute(stmt)

    async def set_title(self, thread_id: str, title: str) -> None:
        async with self.session_factory() as session, session.begin():
            stmt = update(self.model).where(self.model.thread_id == thread_id).values(title=title)
            await session.execute(stmt)

    async def get_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            stmt = select(self.model).where(self.model.thread_id == thread_id)
            if not include_deleted:
                stmt = stmt.where(self.model.is_deleted.is_(False))
            if user_id is not None:
                stmt = stmt.where((self.model.user_id.is_(None)) | (self.model.user_id == user_id))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return thread_row_to_dict(row) if row is not None else None

    async def list_threads(
        self,
        *,
        user_id: str | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            stmt = select(self.model)
            if not include_deleted:
                stmt = stmt.where(self.model.is_deleted.is_(False))
            if user_id is not None:
                stmt = stmt.where((self.model.user_id.is_(None)) | (self.model.user_id == user_id))
            stmt = stmt.order_by(self.model.last_active_at.desc(), self.model.id.desc())
            if offset:
                stmt = stmt.offset(offset)
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [thread_row_to_dict(r) for r in rows]

    async def delete_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        hard: bool = False,
    ) -> bool:
        async with self.session_factory() as session, session.begin():
            stmt = select(self.model).where(self.model.thread_id == thread_id)
            if user_id is not None:
                stmt = stmt.where((self.model.user_id.is_(None)) | (self.model.user_id == user_id))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return False

            if hard:
                await session.execute(delete(self.model).where(self.model.thread_id == thread_id))
            else:
                row.is_deleted = True
                row.deleted_at = datetime.now(UTC)
            return True


__all__ = [
    "BaseThreadStore",
    "InMemoryThreadStore",
    "SQLAlchemyThreadStore",
    "thread_row_to_dict",
]
