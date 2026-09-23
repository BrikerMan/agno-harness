"""SQLRunEventLog — durable frames, deliberately no live tail.

This implements :class:`RunEventLog` and not :class:`RunEventStream`, and the
omission is the design. Tailing over SQL means polling: pick an interval, then
choose between latency the user notices and a query rate the database notices.
Nobody wants to own that loop, and the honest alternative is to say so — with
only this configured, a reload shows history and does not follow a run in
progress, and ``X-Agui-Resume: history`` tells the client exactly that.

What it does buy is real: a background run survives the connection that started
it and is still there after the process restarts.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..core.log import Frame, FrameKind, RunRecord, RunStatus, select_frames
from .mixins import load_json, store_json


class SQLRunEventLog:
    """A :class:`RunEventLog` over models built with the run mixins.

    Parameters
    ----------
    session_factory:
        An async session factory over your own engine.
    frame_model:
        Your model composed with ``RunFrameMixin``.
    run_model:
        Your model composed with ``RunRecordMixin``.
    """

    is_durable = True

    def __init__(
        self,
        session_factory: async_sessionmaker[Any],
        frame_model: type[Any],
        run_model: type[Any],
    ) -> None:
        self._session_factory = session_factory
        self._frames = frame_model
        self._runs = run_model

    # ── frames ────────────────────────────────────────────────────────────

    async def append(self, run_id: str, frames: Sequence[Mapping[str, Any]]) -> str:
        if not frames:
            return ""
        async with self._session_factory() as session, session.begin():
            last = await session.scalar(
                select(func.max(self._frames.sequence)).where(self._frames.run_id == run_id)
            )
            sequence = int(last or 0)
            thread_id = await session.scalar(
                select(self._runs.thread_id).where(self._runs.run_id == run_id)
            )
            for event in frames:
                sequence += 1
                session.add(
                    self._frames(
                        thread_id=thread_id or "",
                        run_id=run_id,
                        sequence=sequence,
                        kind=FrameKind.DELTA.value,
                        event_json=store_json(dict(event)),
                    )
                )
        return _offset(sequence)

    async def read(self, run_id: str, *, after: str | None = None) -> list[Frame]:
        async with self._session_factory() as session:
            query = (
                select(self._frames)
                .where(self._frames.run_id == run_id)
                .order_by(self._frames.sequence)
            )
            if after:
                query = query.where(self._frames.sequence > _sequence(after))
            rows = (await session.execute(query)).scalars()
            frames = [
                Frame(
                    offset=_offset(row.sequence),
                    event=_event(row.event_json),
                    kind=FrameKind(row.kind),
                )
                for row in rows
            ]
        return select_frames(frames)

    # ── run records ───────────────────────────────────────────────────────

    async def start_run(
        self, run_id: str, thread_id: str, *, user_id: str | None = None, **meta: Any
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(select(self._runs).where(self._runs.run_id == run_id))
            if row is None:
                row = self._runs(
                    thread_id=thread_id,
                    run_id=run_id,
                    user_id=user_id,
                    status=RunStatus.RUNNING.value,
                    meta_json=store_json(meta) if meta else None,
                )
                session.add(row)
                await session.flush()
                return _to_record(row)
            record = _to_record(row)
            if record.is_open:
                return record
            await session.execute(delete(self._frames).where(self._frames.run_id == run_id))
            now = datetime.now(UTC)
            row.status = RunStatus.RUNNING.value
            row.error = None
            row.unrecordable = False
            row.updated_at = now
            if hasattr(row, "created_at"):
                row.created_at = now
            if user_id is not None:
                row.user_id = user_id
            row.meta_json = store_json(meta) if meta else None
            await session.flush()
            return _to_record(row)

    async def set_status(self, run_id: str, status: RunStatus, **meta: Any) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(select(self._runs).where(self._runs.run_id == run_id))
            if row is None:
                return
            row.status = status.value
            if "error" in meta:
                row.error = meta.pop("error")
            if "unrecordable" in meta:
                row.unrecordable = bool(meta.pop("unrecordable"))
            if meta:
                current = load_json(row.meta_json)
                merged = {**(current if isinstance(current, dict) else {}), **meta}
                row.meta_json = store_json(merged)

    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self._session_factory() as session:
            row = await session.scalar(select(self._runs).where(self._runs.run_id == run_id))
            return _to_record(row) if row is not None else None

    async def list_runs(self, thread_id: str, *, user_id: str | None = None) -> list[RunRecord]:
        async with self._session_factory() as session:
            query = select(self._runs).where(self._runs.thread_id == thread_id)
            if user_id is not None:
                # Pushed into the query rather than filtered afterwards: a
                # thread id is guessable, and a miss should be indistinguishable
                # from a thread that does not exist.
                query = query.where(self._runs.user_id == user_id)
            rows = (await session.execute(query.order_by(self._runs.id))).scalars()
            return [_to_record(row) for row in rows]

    async def heartbeat(self, run_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(select(self._runs).where(self._runs.run_id == run_id))
            if row is not None:
                row.updated_at = datetime.now(UTC)

    async def delete_thread(self, thread_id: str) -> int:
        async with self._session_factory() as session, session.begin():
            await session.execute(delete(self._frames).where(self._frames.thread_id == thread_id))
            result = await session.execute(
                delete(self._runs).where(self._runs.thread_id == thread_id)
            )
            return int(result.rowcount or 0)


def _offset(sequence: int) -> str:
    """Zero-padded so lexical comparison matches numeric comparison.

    Clients compare offsets as opaque strings, and Redis Stream IDs already sort
    that way; padding here means a client never has to know which backend it is
    talking to.
    """
    return f"{sequence:012d}"


def _sequence(offset: str) -> int:
    try:
        return int(offset)
    except ValueError:
        return 0


def _to_record(row: Any) -> RunRecord:
    return RunRecord(
        run_id=row.run_id,
        thread_id=row.thread_id,
        user_id=row.user_id,
        status=RunStatus(row.status),
        started_at=_epoch(getattr(row, "created_at", None)),
        updated_at=_epoch(getattr(row, "updated_at", None)),
        unrecordable=bool(row.unrecordable),
        error=row.error,
        meta=_meta(row.meta_json),
    )


def _event(value: Any) -> dict[str, Any]:
    loaded = load_json(value)
    return loaded if isinstance(loaded, dict) else {}


def _meta(value: Any) -> dict[str, Any]:
    loaded = load_json(value)
    return loaded if isinstance(loaded, dict) else {}


def _epoch(value: Any) -> float:
    """Datetime to epoch seconds, treating naive values as UTC.

    SQLite has no timezone type, so a column written as aware comes back naive
    and ``timestamp()`` would silently read it as local time — an offset that
    only shows up as runs appearing hours out of order for anyone east or west
    of the machine that wrote them.
    """
    if value is None:
        return time.time()
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return float(value.timestamp())
    except (AttributeError, TypeError, ValueError):
        return time.time()


__all__ = ["SQLRunEventLog"]
