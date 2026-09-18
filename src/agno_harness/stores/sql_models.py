from __future__ import annotations

from typing import Any

from sqlalchemy.orm import DeclarativeBase

from .mixins import ActionRecordMixin, MessageAuditMixin, SessionRecordMixin


class DefaultRelayBase(DeclarativeBase):
    """Default declarative base for dynamically created relay models."""


_DYNAMIC_MODELS: dict[tuple[str, str], type[Any]] = {}


def get_or_create_session_model(
    table_name: str = "agno_conversation_sessions",
    base: Any | None = None,
) -> type[Any]:
    """Return an ORM model for session records with the given table name."""
    key = ("session", table_name)
    if key in _DYNAMIC_MODELS:
        return _DYNAMIC_MODELS[key]
    actual_base = base or DefaultRelayBase
    model_cls = type(
        f"SessionRecord_{table_name.replace('-', '_').replace('.', '_')}",
        (actual_base, SessionRecordMixin),
        {"__tablename__": table_name, "__table_args__": {"extend_existing": True}},
    )
    _DYNAMIC_MODELS[key] = model_cls
    return model_cls


def get_or_create_action_model(
    table_name: str = "agno_interactive_actions",
    base: Any | None = None,
) -> type[Any]:
    """Return an ORM model for interactive action tracking with the given table name."""
    key = ("action", table_name)
    if key in _DYNAMIC_MODELS:
        return _DYNAMIC_MODELS[key]
    actual_base = base or DefaultRelayBase
    model_cls = type(
        f"ActionRecord_{table_name.replace('-', '_').replace('.', '_')}",
        (actual_base, ActionRecordMixin),
        {"__tablename__": table_name, "__table_args__": {"extend_existing": True}},
    )
    _DYNAMIC_MODELS[key] = model_cls
    return model_cls


def get_or_create_audit_model(
    table_name: str = "agno_message_audits",
    base: Any | None = None,
) -> type[Any]:
    """Return an ORM model for message audits with the given table name."""
    key = ("audit", table_name)
    if key in _DYNAMIC_MODELS:
        return _DYNAMIC_MODELS[key]
    actual_base = base or DefaultRelayBase
    model_cls = type(
        f"MessageAudit_{table_name.replace('-', '_').replace('.', '_')}",
        (actual_base, MessageAuditMixin),
        {"__tablename__": table_name, "__table_args__": {"extend_existing": True}},
    )
    _DYNAMIC_MODELS[key] = model_cls
    return model_cls


__all__ = [
    "DefaultRelayBase",
    "get_or_create_action_model",
    "get_or_create_audit_model",
    "get_or_create_session_model",
]
