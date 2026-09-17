import abc
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .models import ConversationKey, SessionRecord


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


class SessionManager:
    """Manages Ivy-style session boundaries and 25-hour idle auto-close lifecycle."""

    def __init__(
        self,
        store: BaseSessionStore | None = None,
        idle_ttl: timedelta = timedelta(hours=25),
    ) -> None:
        self.store = store or InMemorySessionStore()
        self.idle_ttl = idle_ttl

    async def get_or_create_session(
        self, key: ConversationKey, metadata: dict[str, Any] | None = None
    ) -> tuple[str, bool]:
        """Resolve the active AGNO session ID for a conversation.

        Returns:
            (agno_session_id, is_new_session)
        """
        s_key = key.session_key
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
        s_key = key.session_key
        record = await self.store.get(s_key)
        if record is not None and record.finished_at is None:
            record.finished_at = datetime.now(UTC)
            record.metadata["close_reason"] = reason
            await self.store.save(record)
