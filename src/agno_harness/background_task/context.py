"""The chat that is asking, so a background task can reply there later."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from ..sessions.models import ConversationKey

_origin: ContextVar[dict[str, Any] | None] = ContextVar("agno_background_origin", default=None)


def bind_origin(platform: str, key: ConversationKey) -> Token[dict[str, Any] | None]:
    """Remember the inbound chat for the current turn."""
    return _origin.set({"platform": platform, "key": key.model_dump(mode="json")})


def reset_origin(token: Token[dict[str, Any] | None]) -> None:
    _origin.reset(token)


def current_origin() -> dict[str, Any] | None:
    """Platform name and conversation key captured for this turn."""
    return _origin.get()
