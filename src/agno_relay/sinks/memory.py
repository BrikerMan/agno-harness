from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey
from .base import BaseSink


@dataclass
class AuditRecord:
    direction: str  # "inbound" | "outbound"
    platform: str
    chat_id: str
    session_id: str
    text: str
    cards: list[Any] = field(default_factory=list)
    sender_id: str | None = None
    thread_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class InMemorySink(BaseSink):
    """In-memory event sink for testing and debugging."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def record_inbound(self, event: ChannelEvent, session_id: str) -> None:
        self.records.append(
            AuditRecord(
                direction="inbound",
                platform=event.key.platform,
                chat_id=event.key.chat_id,
                session_id=session_id,
                text=event.text,
                sender_id=event.key.sender_id,
                thread_id=event.key.thread_id,
            )
        )

    async def record_outbound(
        self, destination: ConversationKey, message: OutboundMessage, session_id: str
    ) -> None:
        self.records.append(
            AuditRecord(
                direction="outbound",
                platform=destination.platform,
                chat_id=destination.chat_id,
                session_id=session_id,
                text=message.text,
                cards=message.cards,
                sender_id=destination.sender_id,
                thread_id=destination.thread_id,
            )
        )
