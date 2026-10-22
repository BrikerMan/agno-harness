"""Background task rows in ``agno_harness_background_tasks``.

The name is fixed. It is not prefixed with the app name, and this store does not
create or alter the app-prefixed harness tables.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect, select

from ..stores.mixins import load_json, store_json
from ..stores.sql_models import BACKGROUND_TASKS_TABLE, register_background_task_model

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
ACTIVE = (PENDING, RUNNING)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class TaskRecord:
    __slots__ = (
        "id",
        "task_type",
        "subject",
        "params",
        "status",
        "result",
        "error",
        "origin",
        "created_at",
        "started_at",
        "completed_at",
        "attempts",
    )

    def __init__(
        self,
        *,
        id: int,
        task_type: str,
        subject: str,
        params: dict[str, Any],
        status: str,
        result: str | None,
        error: str | None,
        origin: dict[str, Any] | None,
        created_at: str,
        started_at: str | None,
        completed_at: str | None,
        attempts: int = 0,
    ) -> None:
        self.id = id
        self.task_type = task_type
        self.subject = subject
        self.params = params
        self.status = status
        self.result = result
        self.error = error
        self.origin = origin
        self.created_at = created_at
        self.started_at = started_at
        self.completed_at = completed_at
        self.attempts = attempts


def _record(row: Any) -> TaskRecord:
    created = _iso(row.created_at)
    return TaskRecord(
        id=int(row.id),
        task_type=row.task_type,
        subject=row.subject,
        params=load_json(row.params_json) or {},
        status=row.status,
        result=row.result,
        error=row.error,
        origin=load_json(row.origin_json),
        created_at=created or "",
        started_at=_iso(row.started_at),
        completed_at=_iso(row.completed_at),
        attempts=int(row.attempts or 0),
    )


class BackgroundTaskStore:
    """Rows in ``agno_harness_background_tasks``. ``db`` is an ``AgnoHarnessDb``."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._model = register_background_task_model()
        self._table_ready = False

    async def _ready(self) -> None:
        if self._table_ready:
            return
        engine = self._db.engine
        async with engine.begin() as conn:
            names = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            if BACKGROUND_TASKS_TABLE not in names:
                if "alembic_version" in names:
                    raise RuntimeError(
                        f"{BACKGROUND_TASKS_TABLE} is missing. Run alembic upgrade head."
                    )
                await conn.run_sync(
                    lambda sync: self._model.__table__.create(sync, checkfirst=True)
                )
        self._table_ready = True

    async def insert(
        self,
        *,
        task_type: str,
        subject: str,
        params: dict[str, Any],
        origin: dict[str, Any] | None,
    ) -> TaskRecord:
        await self._ready()
        model = self._model
        async with self._db.session_factory() as session, session.begin():
            row = model(
                task_type=task_type,
                subject=subject,
                params_json=store_json(params),
                status=PENDING,
                origin_json=store_json(origin) if origin is not None else None,
                created_at=_now(),
                attempts=0,
            )
            session.add(row)
            await session.flush()
            task_id = int(row.id)
        found = await self.get(task_id)
        if found is None:
            raise RuntimeError(f"background task #{task_id} was not stored")
        return found

    async def get(self, task_id: int) -> TaskRecord | None:
        await self._ready()
        async with self._db.session_factory() as session:
            row = await session.get(self._model, task_id)
        return _record(row) if row is not None else None

    async def list_active(self) -> list[TaskRecord]:
        await self._ready()
        model = self._model
        async with self._db.session_factory() as session:
            rows = (
                await session.scalars(
                    select(model).where(model.status.in_(ACTIVE)).order_by(model.id)
                )
            ).all()
        return [_record(row) for row in rows]

    async def _update(self, task_id: int, **values: Any) -> None:
        await self._ready()
        async with self._db.session_factory() as session, session.begin():
            row = await session.get(self._model, task_id)
            if row is None:
                return
            for key, value in values.items():
                setattr(row, key, value)

    async def mark_running(self, task_id: int) -> None:
        await self._ready()
        async with self._db.session_factory() as session, session.begin():
            row = await session.get(self._model, task_id)
            if row is None:
                return
            row.status = RUNNING
            row.started_at = _now()
            row.attempts = int(row.attempts or 0) + 1

    async def mark_done(self, task_id: int, result: str) -> None:
        await self._update(task_id, status=DONE, result=result, completed_at=_now())

    async def mark_failed(self, task_id: int, error: str) -> None:
        await self._update(task_id, status=FAILED, error=error, completed_at=_now())
