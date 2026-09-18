from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ConversationKey(BaseModel):
    """Unified conversation topology descriptor across Web, CLI, Teams, and Lark."""

    model_config = ConfigDict(extra="allow")

    platform: str
    chat_id: str
    thread_id: str | None = None
    reply_to_id: str | None = None
    sender_id: str | None = None
    tenant_id: str | None = None
    is_direct_message: bool = False

    @property
    def session_key(self) -> str:
        """Stable default session boundary key.

        - 1-on-1 Direct Message: whole chat is one continuous session.
        - Thread / Channel: per-person per-thread isolation.
        - Group chat: per-person inside the group chat.
        """
        if self.is_direct_message:
            return f"{self.platform}:{self.chat_id}"
        sender = self.sender_id or "anonymous"
        if self.thread_id:
            return f"{self.platform}:{self.chat_id}:{self.thread_id}:{sender}"
        return f"{self.platform}:{self.chat_id}:{sender}"


class SessionRecord(BaseModel):
    """Persistent tracking record for a conversation session."""

    model_config = ConfigDict(extra="allow")

    session_key: str
    agno_session_id: str = Field(default_factory=lambda: str(uuid4()))
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, ttl_seconds: float) -> bool:
        """Check whether session has been idle longer than the TTL."""
        if self.finished_at is not None:
            return True
        now = datetime.now(UTC)
        elapsed = (now - self.last_active_at).total_seconds()
        return elapsed > ttl_seconds
