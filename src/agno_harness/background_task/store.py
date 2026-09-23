"""SQLite rows for background tasks. One file, shared with the agent database."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
ACTIVE = (PENDING, RUNNING)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class TaskRecord:
    id: int
    task_type: str
    subject: str
    params: dict[str, Any]
    status: str
    result: str | None
    error: str | None
    origin: dict[str, Any] | None
    created_at: str
    started_at: str | None
    completed_at: str | None


def _row(row: aiosqlite.Row) -> TaskRecord:
    origin_raw = row["origin_json"]
    return TaskRecord(
        id=int(row["id"]),
        task_type=row["task_type"],
        subject=row["subject"],
        params=json.loads(row["params_json"] or "{}"),
        status=row["status"],
        result=row["result"],
        error=row["error"],
        origin=json.loads(origin_raw) if origin_raw else None,
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


class BackgroundTaskStore:
    """Persist task status in the process sqlite file."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ready = False

    async def _db(self) -> aiosqlite.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        if not self._ready:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS background_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    origin_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            await db.commit()
            self._ready = True
        return db

    async def insert(
        self,
        *,
        task_type: str,
        subject: str,
        params: dict[str, Any],
        origin: dict[str, Any] | None,
    ) -> TaskRecord:
        db = await self._db()
        try:
            cursor = await db.execute(
                """
                INSERT INTO background_tasks (
                    task_type, subject, params_json, status, origin_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_type,
                    subject,
                    json.dumps(params, ensure_ascii=False),
                    PENDING,
                    json.dumps(origin, ensure_ascii=False) if origin else None,
                    _now(),
                ),
            )
            await db.commit()
            task_id = int(cursor.lastrowid or 0)
        finally:
            await db.close()
        found = await self.get(task_id)
        if found is None:
            raise RuntimeError(f"background task #{task_id} was not stored")
        return found

    async def get(self, task_id: int) -> TaskRecord | None:
        db = await self._db()
        try:
            cursor = await db.execute("SELECT * FROM background_tasks WHERE id = ?", (task_id,))
            row = await cursor.fetchone()
        finally:
            await db.close()
        return _row(row) if row is not None else None

    async def list_active(self) -> list[TaskRecord]:
        db = await self._db()
        try:
            cursor = await db.execute(
                "SELECT * FROM background_tasks WHERE status IN (?, ?) ORDER BY id",
                ACTIVE,
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()
        return [_row(row) for row in rows]

    async def mark_running(self, task_id: int) -> None:
        db = await self._db()
        try:
            await db.execute(
                "UPDATE background_tasks SET status = ?, started_at = ? WHERE id = ?",
                (RUNNING, _now(), task_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def mark_done(self, task_id: int, result: str) -> None:
        db = await self._db()
        try:
            await db.execute(
                """
                UPDATE background_tasks
                SET status = ?, result = ?, completed_at = ?
                WHERE id = ?
                """,
                (DONE, result, _now(), task_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def mark_failed(self, task_id: int, error: str) -> None:
        db = await self._db()
        try:
            await db.execute(
                """
                UPDATE background_tasks
                SET status = ?, error = ?, completed_at = ?
                WHERE id = ?
                """,
                (FAILED, error, _now(), task_id),
            )
            await db.commit()
        finally:
            await db.close()
