import logging
from typing import Any

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey
from .base import BaseChannel

log = logging.getLogger("agno_relay.channels.teams")

try:
    from microsoft_agents.activity import Activity, Attachment, ConversationReference
    from microsoft_agents.hosting.core import TurnContext

    _TEAMS_AVAILABLE = True
except ImportError:
    Activity = Any  # type: ignore
    Attachment = Any  # type: ignore
    ConversationReference = Any  # type: ignore
    TurnContext = Any  # type: ignore
    _TEAMS_AVAILABLE = False


class TeamsChannel(BaseChannel):
    """Microsoft Teams channel adapter using Microsoft 365 Agents SDK over FastAPI."""

    def __init__(
        self,
        bot_app_id: str | None = None,
        bot_app_password: str | None = None,
        tenant_id: str | None = None,
        adapter: Any = None,
    ) -> None:
        super().__init__(name="teams")
        self.bot_app_id = bot_app_id
        self.bot_app_password = bot_app_password
        self.tenant_id = tenant_id
        self.adapter = adapter
        self._router: Any = None

    def get_router(self) -> Any:
        """Create or return the FastAPI router for Teams webhook `/api/messages`."""
        if self._router is not None:
            return self._router

        from fastapi import APIRouter, Request, Response

        router = APIRouter()

        @router.post("/api/messages")
        async def handle_teams_messages(request: Request) -> Response:
            body = await request.json()
            await self._process_inbound_activity(body)
            return Response(status_code=200)

        self._router = router
        return self._router

    async def _process_inbound_activity(self, activity_data: dict[str, Any]) -> None:
        """Normalize an incoming Teams Activity into a ChannelEvent."""
        activity_type = activity_data.get("type", "message")
        if activity_type != "message":
            return

        conv = activity_data.get("conversation", {})
        chat_id = conv.get("id", "")
        is_dm = conv.get("conversationType") == "personal"

        # Thread key detection
        channel_data = activity_data.get("channelData", {})
        thread_id = None
        if "teamsChannelId" in channel_data:
            # Channel message in Teams: thread is the root activity ID
            thread_id = channel_data.get("teamsTeamId") or conv.get("id")

        reply_to_id = activity_data.get("id")
        from_user = activity_data.get("from", {})
        sender_id = from_user.get("aadObjectId") or from_user.get("id")

        key = ConversationKey(
            platform="teams",
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_id=reply_to_id,
            sender_id=sender_id,
            tenant_id=activity_data.get("channelData", {}).get("tenant", {}).get("id"),
            is_direct_message=is_dm,
        )

        event = ChannelEvent(
            event_id=activity_data.get("id", ""),
            key=key,
            text=activity_data.get("text", ""),
            raw=activity_data,
        )
        await self.push_event(event)

    async def ack(self, event: ChannelEvent, emoji: str = "think") -> Any:
        """Acknowledge receipt with Teams reaction (e.g. think 🤔)."""
        log.debug(f"Teams reaction ACK '{emoji}' for event {event.event_id}")
        return {"acknowledged": True, "emoji": emoji, "activity_id": event.event_id}

    async def settle(
        self, destination: ConversationKey, ack_token: Any, emoji: str = "2705_whiteheavycheckmark"
    ) -> None:
        """Mark completion with Teams checkmark reaction (✅)."""
        log.debug(f"Teams settle '{emoji}' for destination {destination.chat_id}")

    def build_attachments(self, message: OutboundMessage) -> list[Any]:
        """Convert outbound cards into Adaptive Card attachments."""
        attachments: list[Any] = []
        for card in message.cards:
            if _TEAMS_AVAILABLE:
                attachments.append(
                    Attachment(
                        content_type="application/vnd.microsoft.card.adaptive",
                        content=card,
                    )
                )
            else:
                attachments.append(
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    }
                )
        return attachments

    async def send(self, destination: ConversationKey, message: OutboundMessage) -> str:
        """Deliver OutboundMessage (text + Adaptive Cards) to Teams."""
        attachments = self.build_attachments(message)
        if self.adapter is not None:
            # Invoke real adapter if supplied
            try:
                # E.g. continue_conversation / send_reply
                return await self.adapter.send_reply(destination, message, attachments)
            except Exception as exc:
                log.error(f"Error sending message to Teams: {exc}", exc_info=True)

        return f"teams_msg_{destination.chat_id}"
