import abc

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey


class BaseSink(abc.ABC):
    """Abstract interface for auditing messages into storage."""

    @abc.abstractmethod
    async def record_inbound(self, event: ChannelEvent, session_id: str) -> None:
        """Record an inbound user message or interactive action."""
        ...

    @abc.abstractmethod
    async def record_outbound(
        self, destination: ConversationKey, message: OutboundMessage, session_id: str
    ) -> None:
        """Record an outbound assistant response and associated cards."""
        ...
