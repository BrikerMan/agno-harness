"""InMemoryRunEventLog — both protocols, one process, nothing on disk.

The default for tests and the demo. A background run survives a dropped
connection and can be resumed from an offset; it does not survive a restart,
which for a single-process demo is the honest trade.

Offsets are ``"{sequence}"`` zero-padded so that string ordering matches numeric
ordering. Redis Stream IDs already have that property and SQL rowids are given
it, so a client can compare offsets without knowing which backend produced them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ..core.log import Frame, FrameKind, RunRecord, RunStatus, select_frames


class InMemoryRunEventLog:
    """A :class:`RunEventLog` and :class:`RunEventStream` backed by dicts."""

    #: Frames do not survive a restart, so this must not be the only place a
    #: conversation lives — see ``Stores.is_durable``.
    is_durable = False

    def __init__(self) -> None:
        self._frames: dict[str, list[Frame]] = {}
        self._runs: dict[str, RunRecord] = {}
        # One event per run, replaced on every append. A waiter grabs the
        # current event before checking for new frames, so a frame appended
        # between the check and the wait still wakes it.
        self._wakeups: dict[str, asyncio.Event] = {}
        self._sequence = 0

    # ── RunEventLog ───────────────────────────────────────────────────────

    async def append(self, run_id: str, frames: Sequence[Mapping[str, Any]]) -> str:
        stored = self._frames.setdefault(run_id, [])
        offset = stored[-1].offset if stored else ""
        for event in frames:
            self._sequence += 1
            offset = f"{self._sequence:012d}"
            stored.append(Frame(offset=offset, event=dict(event), kind=FrameKind.DELTA))
        record = self._runs.get(run_id)
        if record is not None:
            record.updated_at = time.time()
        self._wake(run_id)
        return offset

    async def read(self, run_id: str, *, after: str | None = None) -> list[Frame]:
        frames = select_frames(self._frames.get(run_id, []))
        if after is None:
            return list(frames)
        return [frame for frame in frames if frame.offset > after]

    async def start_run(
        self, run_id: str, thread_id: str, *, user_id: str | None = None, **meta: Any
    ) -> RunRecord:
        existing = self._runs.get(run_id)
        if existing is not None and existing.is_open:
            return existing
        now = time.time()
        if existing is not None:
            existing.status = RunStatus.RUNNING
            existing.error = None
            existing.unrecordable = False
            existing.started_at = now
            existing.updated_at = now
            existing.user_id = user_id if user_id is not None else existing.user_id
            existing.meta = dict(meta)
            self._frames[run_id] = []
            self._wake(run_id)
            return existing
        record = RunRecord(
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id,
            status=RunStatus.RUNNING,
            started_at=now,
            updated_at=now,
            meta=dict(meta),
        )
        self._runs[run_id] = record
        return record

    async def set_status(self, run_id: str, status: RunStatus, **meta: Any) -> None:
        record = self._runs.get(run_id)
        if record is None:
            return
        record.status = status
        record.updated_at = time.time()
        if "error" in meta:
            record.error = meta.pop("error")
        if "unrecordable" in meta:
            record.unrecordable = bool(meta.pop("unrecordable"))
        record.meta.update(meta)
        self._wake(run_id)

    async def get_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    async def list_runs(self, thread_id: str, *, user_id: str | None = None) -> list[RunRecord]:
        return [
            record
            for record in self._runs.values()
            if record.thread_id == thread_id and (user_id is None or record.user_id == user_id)
        ]

    async def heartbeat(self, run_id: str) -> None:
        record = self._runs.get(run_id)
        if record is not None:
            record.updated_at = time.time()

    async def delete_thread(self, thread_id: str) -> int:
        doomed = [rid for rid, r in self._runs.items() if r.thread_id == thread_id]
        for run_id in doomed:
            self._runs.pop(run_id, None)
            self._frames.pop(run_id, None)
            self._wakeups.pop(run_id, None)
        return len(doomed)

    # ── RunEventStream ────────────────────────────────────────────────────

    async def tail(self, run_id: str, *, after: str | None = None) -> AsyncIterator[Frame]:
        cursor = after
        while True:
            # Take the wakeup *before* reading, so an append that lands between
            # the read and the wait is not missed.
            wakeup = self._wakeups.setdefault(run_id, asyncio.Event())
            pending = await self.read(run_id, after=cursor)
            for frame in pending:
                cursor = frame.offset
                yield frame
            record = self._runs.get(run_id)
            if record is not None and not record.is_producing and not pending:
                return
            if pending:
                continue
            await wakeup.wait()

    def _wake(self, run_id: str) -> None:
        wakeup = self._wakeups.get(run_id)
        if wakeup is not None:
            wakeup.set()
        self._wakeups[run_id] = asyncio.Event()


__all__ = ["InMemoryRunEventLog"]
