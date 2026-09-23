"""Column mixins for the records the toolbox needs to persist.

The toolbox owns no tables. It declares the columns it must be able to read and
write, and you compose them onto your own declarative ``Base`` with your own
table name, your own naming convention, and any extra columns you want::

    class AgentCustomEvent(Base, CustomEventMixin):
        __tablename__ = "agent_custom_events"
        tenant_id: Mapped[str] = mapped_column(String(64), index=True)

That way the records live in the agent project's Alembic migrations, next to
the rest of its schema, instead of in a private table the library creates
behind your back. Do not call ``metadata.create_all`` for these tables.

If the columns do not suit you at all, skip the mixins and implement the store
protocols in :mod:`agno_harness.persistence.stores` directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# High-performance column variant: native binary JSONB on PostgreSQL (supports GIN indexes & ->> operators),
# graceful fallback to JSON/TEXT on SQLite and MySQL.
JSONVariant = JSON().with_variant(JSONB, "postgresql")


def store_json(value: Any) -> Any:
    """A JSON document for :data:`JSONVariant`.

    The column receives an object. PostgreSQL stores ``JSONB``; SQLite stores
    JSON text. ``None`` stays ``None``.
    """
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def load_json(value: Any) -> Any:
    """Read a :data:`JSONVariant` cell.

    PostgreSQL hands back an object. SQLite may hand back that object or the
    original text.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _RecordBase:
    """Identity and timestamps shared by every toolbox record."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class CustomEventMixin(_RecordBase):
    """An injected ``CUSTOM`` event, stored so replay can re-attach it.

    Events injected by a pre-run hook exist only in the live stream — nothing in
    Agno's session store knows about them. Persisting them keyed by ``run_id`` is
    what lets a reloaded thread show the same billing notice, quota warning or
    provenance banner it showed the first time.
    """

    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    value_json: Mapped[Any] = mapped_column(JSONVariant, nullable=False)


class RunFrameMixin(_RecordBase):
    """One AG-UI frame as the client received it.

    ``sequence`` is per-run and monotonic; it becomes the offset a client
    resumes from, so it is the ordering key rather than the primary key. Two
    processes appending to the same run would collide on it, which is exactly
    the situation :class:`LongRunManager` prevents by making ``start`` idempotent.
    """

    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="delta", nullable=False)
    event_json: Mapped[Any] = mapped_column(JSONVariant, nullable=False)


class RunArchiveMixin(_RecordBase):
    """One finished run, as coalesced AG-UI events.

    The hot log (Redis) keeps every delta so a client can resume mid-sentence.
    This row is the durable stand-in written when the run settles: consecutive
    ``TEXT_MESSAGE_CONTENT`` / ``REASONING_MESSAGE_CONTENT`` / ``TOOL_CALL_ARGS``
    deltas of the same message are folded together. Reducing the list still
    yields the conversation the user watched, including sub-agent brackets and
    cards, without a row per token.
    """

    user_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    events_json: Mapped[Any] = mapped_column(JSONVariant, nullable=False)


class RunRecordMixin(_RecordBase):
    """A run's status, so a reload knows whether to wait for more frames.

    ``user_id`` is stored because ownership has to be checkable without reading
    a single frame — the resume and cancel routes are addressed by run id alone,
    and a run id is guessable.
    """

    user_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    unrecordable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)


class ThreadRecordMixin:
    """First-class Thread entity for conversation listings, lifecycle, and UI status."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(256), default="New Chat", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True, nullable=False)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True, nullable=False
    )
    last_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)


class SessionRecordMixin:
    """Conversation session record for managing default boundaries and idle TTL."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(String(256), unique=True, index=True, nullable=False)
    agno_session_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)


class ActionRecordMixin:
    """Interactive card action and HITL approval state tracking.

    Stores session, run, tool_call_id, and metadata when an interactive card is dispatched,
    allowing Teams and Lark button callbacks to look up the full context and record user decisions.
    """

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    session_key: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    agno_session_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )  # "pending", "approved", "rejected", "completed"
    payload_json: Mapped[Any] = mapped_column(JSONVariant, nullable=False)
    meta_json: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)
    result_json: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class MessageAuditMixin:
    """Message audit log recording inbound and outbound messages across all channels."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    chat_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    sender_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    direction: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False
    )  # "inbound" | "outbound"
    text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)
    cards_json: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)
    extra_json: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


def record_to_dict(record: Any) -> dict[str, Any]:
    """Common serialization for the read side of a store."""
    created_at = getattr(record, "created_at", None)
    return {
        "id": getattr(record, "id", None),
        "threadId": getattr(record, "thread_id", None),
        "runId": getattr(record, "run_id", None),
        "createdAt": created_at.isoformat() if created_at else None,
    }


__all__ = [
    "ActionRecordMixin",
    "CustomEventMixin",
    "JSONVariant",
    "load_json",
    "store_json",
    "MessageAuditMixin",
    "RunArchiveMixin",
    "RunFrameMixin",
    "RunRecordMixin",
    "SessionRecordMixin",
    "ThreadRecordMixin",
    "record_to_dict",
]
