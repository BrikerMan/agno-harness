import abc
import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ..core.channel import Channel, ChannelEvent
from ..sessions.models import ConversationKey


class BaseChannel(Channel, abc.ABC):
    """Base class for transport channel adapters."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._inbound_queue: asyncio.Queue[ChannelEvent] = asyncio.Queue()
        self._running = False

    async def listen(self) -> AsyncIterator[ChannelEvent]:
        """Yield events as they arrive in the inbound queue."""
        while self._running or not self._inbound_queue.empty():
            try:
                event = await asyncio.wait_for(self._inbound_queue.get(), timeout=0.5)
                yield event
                self._inbound_queue.task_done()
            except TimeoutError:
                continue

    async def push_event(self, event: ChannelEvent) -> None:
        """Push a normalized event into the channel's listener queue."""
        await self._inbound_queue.put(event)

    async def start(self) -> None:
        """Initialize channel connections or servers."""
        self._running = True

    async def stop(self) -> None:
        """Tear down channel connections."""
        self._running = False

    async def ack(self, event: ChannelEvent, emoji: str = "👀") -> Any:
        """Default no-op acknowledgment."""
        return None

    async def settle(self, destination: ConversationKey, ack_token: Any, emoji: str = "✅") -> None:
        """Default no-op completion."""
        return None

    async def typing(self, destination: ConversationKey, active: bool) -> None:
        """Default no-op typing indicator."""
        return None

    async def stream_chunk(self, destination: ConversationKey, message_id: str, delta: str) -> None:
        """Default no-op incremental streaming chunk."""
        return None
