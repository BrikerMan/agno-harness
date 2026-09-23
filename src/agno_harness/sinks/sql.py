from __future__ import annotations

import contextlib
import json
from typing import Any

from sqlalchemy import select

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey
from ..stores.sql_models import get_or_create_audit_model
from .base import BaseSink


class SQLAlchemySink(BaseSink):
    """Enterprise SQLAlchemy-backed message audit sink.

    Supports custom table names and integration with user-defined declarative Base models.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        model: type[Any] | None = None,
        table_name: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.model = model or get_or_create_audit_model(table_name)

    async def record_inbound(self, event: ChannelEvent, session_id: str) -> None:
        extra_dict: dict[str, Any] = {}
        if event.action_id:
            extra_dict["action_id"] = event.action_id
            extra_dict["action_value"] = event.action_value
        if event.attachments:
            extra_dict["attachments"] = [a.model_dump(mode="json") for a in event.attachments]

        raw_payload_obj: Any = None
        if event.raw is not None:
            if isinstance(event.raw, (dict, list)):
                raw_payload_obj = event.raw
            else:
                try:
                    raw_payload_obj = json.loads(
                        json.dumps(event.raw, default=str, ensure_ascii=False)
                    )
                except Exception:
                    raw_payload_obj = str(event.raw)

        async with self.session_factory() as session, session.begin():
            row = self.model(
                channel=event.key.platform,
                chat_id=event.key.chat_id,
                thread_id=event.key.thread_id,
                sender_id=event.key.sender_id,
                direction="inbound",
                text=event.text,
                raw_text=event.raw_text or event.text,
                raw_payload_json=raw_payload_obj,
                cards_json=None,
                extra_json=extra_dict or None,
            )
            session.add(row)

    async def record_outbound(
        self, destination: ConversationKey, message: OutboundMessage, session_id: str
    ) -> None:
        async with self.session_factory() as session, session.begin():
            row = self.model(
                channel=destination.platform,
                chat_id=destination.chat_id,
                thread_id=destination.thread_id,
                sender_id=destination.sender_id,
                direction="outbound",
                text=message.text,
                raw_text=message.raw_text or message.text,
                raw_payload_json=None,
                cards_json=message.cards or None,
                extra_json=message.extra or None,
            )
            session.add(row)

    async def get_history(self, chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Query audit history for a chat."""
        async with self.session_factory() as session:
            stmt = (
                select(self.model)
                .where(self.model.chat_id == chat_id)
                .order_by(self.model.id.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            result: list[dict[str, Any]] = []
            for r in reversed(rows):
                cards = r.cards_json
                if isinstance(cards, str):
                    with contextlib.suppress(Exception):
                        cards = json.loads(cards)
                extra = r.extra_json
                if isinstance(extra, str):
                    with contextlib.suppress(Exception):
                        extra = json.loads(extra)
                result.append(
                    {
                        "id": r.id,
                        "channel": r.channel,
                        "chat_id": r.chat_id,
                        "direction": r.direction,
                        "text": r.text,
                        "raw_text": getattr(r, "raw_text", None),
                        "cards": cards or [],
                        "extra": extra or {},
                        "createdAt": r.created_at.isoformat() if r.created_at else None,
                    }
                )
            return result


__all__ = ["SQLAlchemySink"]
