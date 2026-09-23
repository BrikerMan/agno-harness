from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from .mixins import (
    ActionRecordMixin,
    CustomEventMixin,
    MessageAuditMixin,
    RunArchiveMixin,
    RunFrameMixin,
    RunRecordMixin,
    SessionRecordMixin,
    ThreadRecordMixin,
)
from .prefix import table_name as prefixed_table


class DefaultRelayBase(DeclarativeBase):
    """Default declarative base for dynamically created relay models."""


_DYNAMIC_MODELS: dict[tuple[str, str, int], type[Any]] = {}

_HARNESS_MODELS: tuple[tuple[str, str, type[Any]], ...] = (
    ("threads", "threads", ThreadRecordMixin),
    ("sessions", "conversation-sessions", SessionRecordMixin),
    ("actions", "actions", ActionRecordMixin),
    ("audits", "message-audits", MessageAuditMixin),
    ("custom_events", "custom-events", CustomEventMixin),
    ("run_frames", "run-frames", RunFrameMixin),
    ("run_records", "run-records", RunRecordMixin),
    ("run_archives", "run-archives", RunArchiveMixin),
)


def _declare(kind: str, mixin: type[Any], name: str, base: Any | None) -> type[Any]:
    actual = base or DefaultRelayBase
    key = (kind, name, id(actual))
    cached = _DYNAMIC_MODELS.get(key)
    if cached is not None:
        return cached
    label = name.replace("-", "_").replace(".", "_")
    model_cls = type(
        f"{kind}_{label}",
        (actual, mixin),
        {"__tablename__": name, "__table_args__": {"extend_existing": True}},
    )
    _DYNAMIC_MODELS[key] = model_cls
    return model_cls


def register_harness_models(
    prefix: str | None = None,
    base: Any | None = None,
) -> dict[str, type[Any]]:
    """Declare every harness table on the project's ``Base``.

    Call this with the same ``Base`` Alembic already uses. ``target_metadata``
    stays ``Base.metadata`` and autogenerate picks up the new tables.
    ``prefix=None`` uses ``agno-harness``. ``prefix="ipv"`` yields
    ``ipv_conversation_sessions`` and the rest of the set.
    """
    if prefix is not None and not isinstance(prefix, str):
        base = prefix
        prefix = None
    return {
        key: _declare(key, mixin, prefixed_table(suffix, prefix=prefix), base)
        for key, suffix, mixin in _HARNESS_MODELS
    }


def harness_metadata(prefix: str | None = None, base: Any | None = None) -> MetaData:
    """Metadata for the harness tables alone.

    ``prefix`` defaults to the ``AGNO_HARNESS_TABLE_PREFIX`` environment
    variable, then to ``agno-harness``. Pass the result to :func:`include_harness`
    so an existing Alembic ``target_metadata`` keeps the project's own tables.
    """
    if prefix is not None and not isinstance(prefix, str):
        base = prefix
        prefix = None
    chosen = prefix if prefix is not None else os.environ.get("AGNO_HARNESS_TABLE_PREFIX") or None
    register_harness_models(chosen, base)
    return (base or DefaultRelayBase).metadata


def include_harness(
    target_metadata: MetaData | Sequence[MetaData],
    prefix: str | None = None,
) -> list[MetaData]:
    """Add harness tables beside the metadata Alembic already uses.

    The project's models stay first. Alembic autogenerate then sees both.
    """
    harness = harness_metadata(prefix)
    existing = [target_metadata] if isinstance(target_metadata, MetaData) else list(target_metadata)
    if any(item is harness for item in existing):
        return existing
    return [*existing, harness]


def numbered_revisions(context: Any, revision: Any, directives: list[Any]) -> None:
    """Number the next revision ``0001``, ``0002`` when the head is empty or numeric.

    Call this at the start of the project's own ``process_revision_directives``.
    A non-numeric head is left unchanged.
    """
    del context
    if not directives:
        return
    head = (revision[0] if revision else None) if isinstance(revision, tuple) else revision
    if not head:
        directives[0].rev_id = "0001"
        return
    text = str(head)
    if text.isdigit():
        directives[0].rev_id = f"{int(text) + 1:04d}"


def get_or_create_thread_model(
    table_name: str | None = None,
    base: Any | None = None,
) -> type[Any]:
    """Return an ORM model for thread records with the given table name."""
    return _declare(
        "threads",
        ThreadRecordMixin,
        table_name or prefixed_table("threads"),
        base,
    )


def get_or_create_session_model(
    table_name: str | None = None,
    base: Any | None = None,
) -> type[Any]:
    """Return an ORM model for session records with the given table name."""
    return _declare(
        "sessions",
        SessionRecordMixin,
        table_name or prefixed_table("conversation-sessions"),
        base,
    )


def get_or_create_action_model(
    table_name: str | None = None,
    base: Any | None = None,
) -> type[Any]:
    """Return an ORM model for interactive action tracking with the given table name."""
    return _declare(
        "actions",
        ActionRecordMixin,
        table_name or prefixed_table("actions"),
        base,
    )


def get_or_create_audit_model(
    table_name: str | None = None,
    base: Any | None = None,
) -> type[Any]:
    """Return an ORM model for message audits with the given table name."""
    return _declare(
        "audits",
        MessageAuditMixin,
        table_name or prefixed_table("message-audits"),
        base,
    )


__all__ = [
    "DefaultRelayBase",
    "ThreadRecordMixin",
    "get_or_create_action_model",
    "get_or_create_audit_model",
    "get_or_create_session_model",
    "get_or_create_thread_model",
    "harness_metadata",
    "include_harness",
    "numbered_revisions",
    "register_harness_models",
]
