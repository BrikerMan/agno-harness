from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..sessions.models import ConversationKey
from .attachment import InboundAttachment


class OutboundMessage(BaseModel):
    """Normalized outgoing message produced by the runtime and message collector."""

    text: str = ""
    cards: list[Any] = Field(default_factory=list)
    reply_to_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    raw_text: str = ""


class ChannelEvent(BaseModel):
    """Normalized incoming event from an external channel."""

    event_id: str
    key: ConversationKey
    text: str = ""
    raw_text: str = ""
    created_at: datetime | None = None
    sender_name: str | None = None
    action_id: str | None = None
    action_value: Any | None = None
    attachments: list[InboundAttachment] = Field(default_factory=list)
    raw: Any = None


@runtime_checkable
class Channel(Protocol):
    """Asynchronous transport channel adapter protocol."""

    name: str

    def listen(self) -> AsyncIterator[ChannelEvent]:
        """Listen for incoming messages, commands, or card action callbacks."""
        ...

    async def send(self, destination: ConversationKey, message: OutboundMessage) -> str:
        """Send a completed message (text and/or card). Returns platform message_id."""
        ...

    async def stream_chunk(self, destination: ConversationKey, message_id: str, delta: str) -> None:
        """Throttled progressive update of an in-flight message."""
        ...

    async def stream_tool(
        self,
        destination: ConversationKey,
        *,
        tool_call_id: str,
        name: str,
        args: Any = None,
        status: str = "started",
    ) -> None:
        """Optional live tool-call update (name + params)."""
        ...

    async def ack(self, event: ChannelEvent, emoji: str = "👀") -> Any:
        """Acknowledge receipt immediately (e.g. Teams reaction, Lark reaction)."""
        ...

    async def settle(self, destination: ConversationKey, ack_token: Any, emoji: str = "✅") -> None:
        """Mark completion on the platform acknowledgment."""
        ...

    async def typing(self, destination: ConversationKey, active: bool) -> None:
        """Start or stop typing indicator heartbeats."""
        ...
