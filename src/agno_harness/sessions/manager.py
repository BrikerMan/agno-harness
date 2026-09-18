import abc
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
from sqlalchemy import select

from ..stores.sql_models import get_or_create_session_model
from .models import ConversationKey, SessionRecord

SessionKeyResolver = Callable[[ConversationKey], str]


def default_session_key_resolver(key: ConversationKey) -> str:
    """Default session boundary key.

    - 1-on-1 Direct Message: whole chat is one continuous session.
    - Thread / Channel: per-person per-thread isolation.
    - Group chat: per-person inside the group chat.
    """
    return key.session_key


class BaseSessionStore(abc.ABC):
    """Abstract interface for storing conversation session records."""

    @abc.abstractmethod
    async def get(self, session_key: str) -> SessionRecord | None:
        """Fetch the active session record for the key."""
        ...

    @abc.abstractmethod
    async def save(self, record: SessionRecord) -> None:
        """Upsert a session record."""
        ...


class InMemorySessionStore(BaseSessionStore):
    """In-memory dictionary store for session tracking."""

    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}

    async def get(self, session_key: str) -> SessionRecord | None:
        return self._records.get(session_key)

    async def save(self, record: SessionRecord) -> None:
        self._records[record.session_key] = record


class SQLiteSessionStore(BaseSessionStore):
    """SQLite-backed persistent store for session records."""

    def __init__(self, db_path: str = "sessions.db") -> None:
        self.db_path = db_path
        self._initialized = False

    async def _ensure_table(self) -> None:
        if self._initialized:
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    session_key TEXT PRIMARY KEY,
                    agno_session_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    finished_at TEXT,
                    metadata_json TEXT
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def get(self, session_key: str) -> SessionRecord | None:
        await self._ensure_table()
        async with (
            aiosqlite.connect(self.db_path) as db,
            db.execute(
                """
                SELECT session_key, agno_session_id, started_at, last_active_at, finished_at, metadata_json
                FROM conversation_sessions
                WHERE session_key = ?
                """,
                (session_key,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
            if not row:
                return None
            return SessionRecord(
                session_key=row[0],
                agno_session_id=row[1],
                started_at=datetime.fromisoformat(row[2]),
                last_active_at=datetime.fromisoformat(row[3]),
                finished_at=datetime.fromisoformat(row[4]) if row[4] else None,
                metadata=json.loads(row[5]) if row[5] else {},
            )

    async def save(self, record: SessionRecord) -> None:
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO conversation_sessions (
                    session_key, agno_session_id, started_at, last_active_at, finished_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    agno_session_id=excluded.agno_session_id,
                    started_at=excluded.started_at,
                    last_active_at=excluded.last_active_at,
                    finished_at=excluded.finished_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    record.session_key,
                    record.agno_session_id,
                    record.started_at.isoformat(),
                    record.last_active_at.isoformat(),
                    record.finished_at.isoformat() if record.finished_at else None,
                    json.dumps(record.metadata),
                ),
            )
            await db.commit()


class SQLAlchemySessionStore(BaseSessionStore):
    """Enterprise SQLAlchemy-backed store for conversation sessions.

    Supports custom table names and integration with user-defined declarative Base models.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        model: type[Any] | None = None,
        table_name: str = "agno_conversation_sessions",
    ) -> None:
        self.session_factory = session_factory
        self.model = model or get_or_create_session_model(table_name)

    async def get(self, session_key: str) -> SessionRecord | None:
        async with self.session_factory() as session:
            stmt = select(self.model).where(self.model.session_key == session_key)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if not row:
                return None
            metadata = json.loads(row.metadata_json) if row.metadata_json else {}
            return SessionRecord(
                session_key=row.session_key,
                agno_session_id=row.agno_session_id,
                started_at=row.started_at,
                last_active_at=row.last_active_at,
                finished_at=row.finished_at,
                metadata=metadata,
            )

    async def save(self, record: SessionRecord) -> None:
        async with self.session_factory() as session, session.begin():
            stmt = select(self.model).where(self.model.session_key == record.session_key)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            meta_str = json.dumps(record.metadata, ensure_ascii=False) if record.metadata else None

            if existing is not None:
                existing.agno_session_id = record.agno_session_id
                existing.started_at = record.started_at
                existing.last_active_at = record.last_active_at
                existing.finished_at = record.finished_at
                existing.metadata_json = meta_str
            else:
                new_row = self.model(
                    session_key=record.session_key,
                    agno_session_id=record.agno_session_id,
                    started_at=record.started_at,
                    last_active_at=record.last_active_at,
                    finished_at=record.finished_at,
                    metadata_json=meta_str,
                )
                session.add(new_row)


class SessionManager:
    """Manages default session boundaries and 25-hour idle auto-close lifecycle.

    Accepts an optional ``resolver`` callback (``SessionKeyResolver``) so downstream
    applications can define their own conversation topology (e.g. shared channel
    threads, tenant-isolated sessions, or ticket-based groupings).
    """

    def __init__(
        self,
        store: BaseSessionStore | None = None,
        idle_ttl: timedelta = timedelta(hours=25),
        resolver: SessionKeyResolver | None = None,
    ) -> None:
        self.store = store or InMemorySessionStore()
        self.idle_ttl = idle_ttl
        self.resolver = resolver or default_session_key_resolver

    async def get_or_create_session(
        self, key: ConversationKey, metadata: dict[str, Any] | None = None
    ) -> tuple[str, bool]:
        """Resolve the active AGNO session ID for a conversation.

        Returns:
            (agno_session_id, is_new_session)
        """
        s_key = self.resolver(key)
        record = await self.store.get(s_key)
        now = datetime.now(UTC)
        ttl_seconds = self.idle_ttl.total_seconds()

        if record is not None and not record.is_expired(ttl_seconds):
            # Session is active and healthy; touch last_active_at
            record.last_active_at = now
            if metadata:
                record.metadata.update(metadata)
            await self.store.save(record)
            return record.agno_session_id, False

        # Session does not exist or expired; create a new one
        new_session_id = str(uuid4())
        new_record = SessionRecord(
            session_key=s_key,
            agno_session_id=new_session_id,
            started_at=now,
            last_active_at=now,
            metadata=metadata or {},
        )
        await self.store.save(new_record)
        return new_session_id, True

    async def close_session(self, key: ConversationKey, reason: str = "user_reset") -> None:
        """Explicitly retire the current session (e.g. for /reset or /new commands)."""
        s_key = self.resolver(key)
        record = await self.store.get(s_key)
        if record is not None and record.finished_at is None:
            record.finished_at = datetime.now(UTC)
            record.metadata["close_reason"] = reason
            await self.store.save(record)
