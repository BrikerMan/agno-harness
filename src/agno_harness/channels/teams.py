"""Microsoft Teams channel: Bot Framework webhook in, Bot Connector reply out.

``TeamsChannel()`` reads ``AGNO_HARNESS_TEAMS_APP_ID`` / ``AGNO_HARNESS_TEAMS_APP_PASSWORD``.
The webhook is ``POST /api/messages`` (a router ``prefix`` is prepended). Inbound
activities are JWT-verified. Replies use the activity's ``serviceUrl`` and a
client-credentials token. Missing credentials or a missing conversation reference
raise; they never return a fake activity id.
"""

import logging
from typing import Any, Protocol, runtime_checkable

from ..config import RelayConfig
from ..core.attachment import InboundAttachment
from ..core.channel import ChannelEvent, OutboundMessage
from ..core.chimein import ChimeInPolicy, MentionOnlyPolicy
from ..core.prompt import parse_sent_at
from ..sessions.models import ConversationKey
from .base import BaseChannel
from .cleaning import teams_html_to_markdown
from .teams_auth import TeamsJwtVerifier
from .teams_connector import (
    TeamsAuthError,
    TeamsChannelError,
    TeamsConnectorClient,
    TeamsConversationRef,
    TeamsCredentialsError,
    TeamsDeliveryError,
    TeamsKeyDiscoveryError,
    TeamsServiceUrlError,
    validate_service_url,
)

log = logging.getLogger("agno_harness.channels.teams")

TEAMS_MESSAGES_PATH = "/api/messages"

_DEFAULT_POLICY: Any = object()
_HANDLED_ACTIVITY_TYPES = frozenset({"message", "invoke"})


def teams_messaging_endpoint(public_base: str = "https://<your-domain>", prefix: str = "") -> str:
    """Absolute Messaging endpoint for Azure Bot configuration.

    The channel route is always ``/api/messages``. ``prefix`` is the prefix passed
    to ``relay.get_router(prefix=...)``. With no prefix the endpoint is
    ``https://host/api/messages``.
    """
    base = public_base.rstrip("/")
    pre = prefix.strip()
    if pre and not pre.startswith("/"):
        pre = "/" + pre
    pre = pre.rstrip("/")
    return f"{base}{pre}{TEAMS_MESSAGES_PATH}"


def teams_topic_id(activity: dict[str, Any]) -> str | None:
    """Session thread id for a Teams activity.

    Channel topic replies use the ``messageid`` embedded in ``conversation.id``.
    A channel root post is scoped to ``teamsChannelId``, not ``teamsTeamId``, so
    two channels in the same team do not share a session. Group chats and DMs
    stay on the conversation id alone.
    """
    conversation = activity.get("conversation") or {}
    conversation_id = str(conversation.get("id") or "")
    if ";messageid=" in conversation_id:
        root = conversation_id.split(";messageid=", 1)[1]
        return root or None
    channel_data = activity.get("channelData") or {}
    if not isinstance(channel_data, dict):
        return None
    conversation_type = (
        conversation.get("conversationType") if isinstance(conversation, dict) else None
    )
    if "teamsChannelId" in channel_data or conversation_type == "channel":
        channel = channel_data.get("channel")
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        topic = channel_data.get("teamsChannelId") or channel_id
        return str(topic) if topic else None
    return None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _account(account_id: str | None, name: str | None) -> dict[str, str] | None:
    if not account_id and not name:
        return None
    account: dict[str, str] = {}
    if account_id:
        account["id"] = account_id
    if name:
        account["name"] = name
    return account


def conversation_ref_from_activity(activity: dict[str, Any]) -> TeamsConversationRef:
    """Build a validated conversation reference from an inbound Activity."""
    service_url = activity.get("serviceUrl")
    if not isinstance(service_url, str) or not service_url.strip():
        raise TeamsServiceUrlError("serviceUrl is missing or malformed")
    normalized = validate_service_url(service_url)
    conversation = _mapping(activity.get("conversation"))
    conversation_id = str(conversation.get("id") or "")
    if not conversation_id:
        raise TeamsDeliveryError("conversation id is missing; cannot deliver to Teams")
    recipient = _mapping(activity.get("recipient"))
    sender = _mapping(activity.get("from"))
    activity_id = activity.get("id")
    return TeamsConversationRef(
        service_url=normalized,
        conversation_id=conversation_id,
        activity_id=str(activity_id) if activity_id else None,
        bot_id=str(recipient["id"]) if recipient.get("id") else None,
        bot_name=str(recipient["name"]) if recipient.get("name") else None,
        user_id=str(sender["id"]) if sender.get("id") else None,
        user_name=str(sender["name"]) if sender.get("name") else None,
    )


def extract_card_action(activity: dict[str, Any]) -> tuple[str | None, Any]:
    """Pull ``action_id`` from an Action.Submit message or an adaptiveCard invoke."""
    value = activity.get("value")
    if not isinstance(value, dict):
        return None, None
    action = value.get("action")
    if isinstance(action, dict):
        data = action.get("data")
        if isinstance(data, dict) and data.get("action_id"):
            return str(data["action_id"]), data
    if value.get("action_id"):
        return str(value["action_id"]), value
    data = value.get("data")
    if isinstance(data, dict) and data.get("action_id"):
        return str(data["action_id"]), data
    return None, value


@runtime_checkable
class TeamsOutbound(Protocol):
    """Optional replacement for the built-in connector (tests or a host adapter)."""

    async def send_reply(
        self,
        destination: ConversationKey,
        message: OutboundMessage,
        attachments: list[Any],
    ) -> str: ...


class TeamsBotAdapter:
    """Official connector adapter: JWT verify, conversation cache, and replies.

    ``TeamsChannel()`` constructs one of these. Pass ``adapter=`` only when a host
    already owns delivery.
    """

    def __init__(
        self,
        app_id: str,
        app_password: str,
        tenant_id: str | None = None,
        *,
        connector: TeamsConnectorClient | None = None,
        verifier: TeamsJwtVerifier | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_password = app_password
        self.tenant_id = _clean(tenant_id)
        self.connector = connector or TeamsConnectorClient(app_id, app_password, self.tenant_id)
        self.verifier = verifier or TeamsJwtVerifier(app_id)
        self._conversations: dict[str, TeamsConversationRef] = {}

    def remember(self, activity: dict[str, Any]) -> TeamsConversationRef:
        ref = conversation_ref_from_activity(activity)
        self._conversations[ref.conversation_id] = ref
        return ref

    def ref_for(self, chat_id: str) -> TeamsConversationRef:
        ref = self._conversations.get(chat_id)
        if ref is None:
            raise TeamsDeliveryError(
                f"No cached Teams conversation for chat_id={chat_id!r}. "
                "The bot can only reply to a conversation it has already received."
            )
        return ref

    async def send_reply(
        self,
        destination: ConversationKey,
        message: OutboundMessage,
        attachments: list[Any],
    ) -> str:
        ref = self.ref_for(destination.chat_id)
        activity: dict[str, Any] = {
            "type": "message",
            "from": _account(ref.bot_id, ref.bot_name),
            "conversation": {"id": ref.conversation_id},
        }
        recipient = _account(ref.user_id, ref.user_name)
        if recipient is not None:
            activity["recipient"] = recipient
        text = message.text or ""
        if text:
            activity["text"] = text
            activity["textFormat"] = "markdown"
        if ref.activity_id:
            activity["replyToId"] = ref.activity_id
        if attachments:
            activity["attachments"] = attachments
        if not text and not attachments:
            raise TeamsDeliveryError("refusing to post an empty Teams activity")
        return await self.connector.post_activity(ref, activity, reply=bool(ref.activity_id))

    async def send_typing(self, destination: ConversationKey) -> None:
        ref = self.ref_for(destination.chat_id)
        activity: dict[str, Any] = {
            "type": "typing",
            "from": _account(ref.bot_id, ref.bot_name),
            "conversation": {"id": ref.conversation_id},
        }
        recipient = _account(ref.user_id, ref.user_name)
        if recipient is not None:
            activity["recipient"] = recipient
        await self.connector.post_activity(ref, activity, reply=False)

    async def aclose(self) -> None:
        await self.connector.aclose()
        await self.verifier.aclose()


def _invoke_response() -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=200,
        content={
            "statusCode": 200,
            "type": "application/vnd.microsoft.activity.invokeResponse",
            "value": {"statusCode": 200},
        },
    )


class TeamsChannel(BaseChannel):
    """Teams transport. Zero-arg init reads ``AGNO_HARNESS_TEAMS_*`` from the environment.

    Group and channel messages use :class:`MentionOnlyPolicy` unless ``chime_in_policy``
    is passed (including ``None`` to disable it). Direct messages always pass that policy.
    ``RelayApp(chime_in_policy=...)`` overrides the channel policy.
    """

    def __init__(
        self,
        bot_app_id: str | None = None,
        bot_app_password: str | None = None,
        tenant_id: str | None = None,
        adapter: TeamsOutbound | None = None,
        *,
        bot_tenant_id: str | None = None,
        chime_in_policy: ChimeInPolicy | None | Any = _DEFAULT_POLICY,
        connector: TeamsConnectorClient | None = None,
        verifier: TeamsJwtVerifier | None = None,
    ) -> None:
        super().__init__(name="teams")
        if (
            tenant_id
            and bot_tenant_id
            and tenant_id.strip()
            and bot_tenant_id.strip()
            and tenant_id.strip() != bot_tenant_id.strip()
        ):
            raise ValueError("pass tenant_id or bot_tenant_id, not conflicting values")
        self.bot_app_id = _clean(bot_app_id or RelayConfig.teams_app_id()) or ""
        self.bot_app_password = _clean(bot_app_password or RelayConfig.teams_app_password()) or ""
        self.tenant_id = _clean(tenant_id or bot_tenant_id or RelayConfig.teams_tenant_id())
        self._custom_adapter = adapter
        self.bot = TeamsBotAdapter(
            self.bot_app_id,
            self.bot_app_password,
            self.tenant_id,
            connector=connector,
            verifier=verifier if verifier is not None else TeamsJwtVerifier(self.bot_app_id),
        )
        if chime_in_policy is _DEFAULT_POLICY:
            self.chime_in_policy: ChimeInPolicy | None = MentionOnlyPolicy()
        else:
            self.chime_in_policy = chime_in_policy
        self._router: Any = None

    def require_credentials(self) -> None:
        """Raise when the channel cannot authenticate inbound calls or post replies."""
        missing: list[str] = []
        if not self.bot_app_id:
            missing.append("AGNO_HARNESS_TEAMS_APP_ID")
        if self._custom_adapter is None and not self.bot_app_password:
            missing.append("AGNO_HARNESS_TEAMS_APP_PASSWORD")
        if missing:
            raise TeamsCredentialsError(
                "Teams channel is not configured. Set " + " and ".join(missing) + "."
            )

    def get_router(self) -> Any:
        """FastAPI router. The webhook path is ``POST /api/messages``."""
        if self._router is not None:
            return self._router

        from fastapi import APIRouter, Request
        from fastapi.responses import JSONResponse, Response

        router = APIRouter()

        @router.post(TEAMS_MESSAGES_PATH)
        async def handle_teams_messages(request: Request) -> Response:
            try:
                self.require_credentials()
            except TeamsCredentialsError as exc:
                return JSONResponse(status_code=503, content={"error": str(exc)})
            try:
                await self.bot.verifier.verify_authorization(request.headers.get("authorization"))
            except TeamsKeyDiscoveryError:
                log.error("Teams JWKS discovery failed")
                return JSONResponse(
                    status_code=503,
                    content={"error": "cannot verify bot framework token"},
                )
            except TeamsAuthError as exc:
                return JSONResponse(status_code=exc.status_code, content={"error": str(exc)})
            except TeamsCredentialsError as exc:
                return JSONResponse(status_code=503, content={"error": str(exc)})
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(status_code=400, content={"error": "invalid json"})
            if not isinstance(body, dict):
                return JSONResponse(
                    status_code=400, content={"error": "activity must be an object"}
                )
            try:
                outcome = await self._process_inbound_activity(body)
            except TeamsServiceUrlError as exc:
                log.warning("Rejected Teams activity: %s", exc)
                return JSONResponse(status_code=400, content={"error": str(exc)})
            except TeamsDeliveryError as exc:
                log.warning("Rejected Teams activity: %s", exc)
                return JSONResponse(status_code=400, content={"error": str(exc)})
            if outcome == "invoke":
                return _invoke_response()
            return Response(status_code=200)

        self._router = router
        return self._router

    async def _process_inbound_activity(self, activity_data: dict[str, Any]) -> str:
        """Normalize a message or invoke. Other activity types are logged and ignored."""
        activity_type = activity_data.get("type") or "message"
        if activity_type not in _HANDLED_ACTIVITY_TYPES:
            log.info("Ignoring Teams activity type=%s", activity_type)
            return "ignored"

        self.bot.remember(activity_data)
        event = self._event_from_activity(activity_data)
        await self.push_event(event)
        return "invoke" if activity_type == "invoke" else "message"

    def _event_from_activity(self, activity_data: dict[str, Any]) -> ChannelEvent:
        conversation = activity_data.get("conversation") or {}
        chat_id = str(conversation.get("id") or "")
        is_dm = conversation.get("conversationType") == "personal"
        channel_data = activity_data.get("channelData") or {}
        tenant = channel_data.get("tenant") if isinstance(channel_data, dict) else None
        tenant_id = None
        if isinstance(tenant, dict) and tenant.get("id"):
            tenant_id = str(tenant["id"])
        elif conversation.get("tenantId"):
            tenant_id = str(conversation["tenantId"])

        from_user = activity_data.get("from") or {}
        sender_id = None
        sender_name = None
        if isinstance(from_user, dict):
            raw_sender = from_user.get("aadObjectId") or from_user.get("id")
            sender_id = str(raw_sender) if raw_sender else None
            sender_name = str(from_user["name"]) if from_user.get("name") else None

        raw_text = activity_data.get("text") or ""
        if not isinstance(raw_text, str):
            raw_text = str(raw_text)
        entities = activity_data.get("entities") or []
        mentions = [
            item for item in entities if isinstance(item, dict) and item.get("type") == "mention"
        ]
        cleaned_text = teams_html_to_markdown(raw_text, mentions=mentions) if raw_text else ""
        action_id, action_value = extract_card_action(activity_data)

        inbound_attachments: list[InboundAttachment] = []
        for attachment in activity_data.get("attachments") or []:
            if isinstance(attachment, dict):
                inbound_attachments.append(
                    InboundAttachment(
                        id=str(attachment["id"]) if attachment.get("id") else None,
                        name=str(attachment.get("name") or ""),
                        content_type=attachment.get("contentType"),
                        url=attachment.get("contentUrl"),
                        raw=attachment,
                    )
                )

        activity_id = activity_data.get("id")
        return ChannelEvent(
            event_id=str(activity_id or ""),
            key=ConversationKey(
                platform="teams",
                chat_id=chat_id,
                thread_id=teams_topic_id(activity_data),
                reply_to_id=str(activity_id) if activity_id else None,
                sender_id=sender_id,
                tenant_id=tenant_id,
                is_direct_message=is_dm,
            ),
            text=cleaned_text,
            raw_text=raw_text,
            created_at=parse_sent_at(activity_data.get("timestamp")),
            sender_name=sender_name,
            action_id=action_id,
            action_value=action_value,
            attachments=inbound_attachments,
            raw=activity_data,
        )

    async def start(self) -> None:
        self.require_credentials()
        await super().start()

    async def stop(self) -> None:
        await super().stop()
        await self.bot.aclose()

    async def ack(self, event: ChannelEvent, emoji: str = "👀") -> Any:
        """Send a typing activity. Teams has no arbitrary emoji reaction on this API."""
        await self.typing(event.key, True)
        return {"activity_id": event.event_id, "emoji": emoji, "kind": "typing"}

    async def settle(self, destination: ConversationKey, ack_token: Any, emoji: str = "✅") -> None:
        """Close the turn locally. The outbound message is the visible result.

        The Bot Connector reaction vocabulary is like/heart/laugh/surprised/sad/angry.
        Stamping one of those on the user's message would not match 👀/✅/❌, so
        settle does not post a reaction. Typing was already sent by ``ack``.
        """
        log.info(
            "Teams turn settled emoji=%s chat=%s ack=%s",
            emoji,
            destination.chat_id,
            ack_token,
        )

    async def typing(self, destination: ConversationKey, active: bool) -> None:
        if not active:
            return
        if self._custom_adapter is not None and hasattr(self._custom_adapter, "send_typing"):
            await self._custom_adapter.send_typing(destination)  # type: ignore[attr-defined]
            return
        await self.bot.send_typing(destination)

    def build_attachments(self, message: OutboundMessage) -> list[Any]:
        """Wrap Adaptive Card dicts. Non-dict cards are left for the text body."""
        attachments: list[Any] = []
        for card in message.cards:
            if isinstance(card, dict):
                attachments.append(
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    }
                )
        return attachments

    async def send(self, destination: ConversationKey, message: OutboundMessage) -> str:
        """Deliver text and Adaptive Cards. Raises when delivery does not happen."""
        self.require_credentials()
        attachments = self.build_attachments(message)
        if self._custom_adapter is not None:
            activity_id = await self._custom_adapter.send_reply(destination, message, attachments)
            if not activity_id:
                raise TeamsDeliveryError("adapter send_reply returned an empty activity id")
            return str(activity_id)
        return await self.bot.send_reply(destination, message, attachments)


__all__ = [
    "TEAMS_MESSAGES_PATH",
    "TeamsAuthError",
    "TeamsBotAdapter",
    "TeamsChannel",
    "TeamsChannelError",
    "TeamsCredentialsError",
    "TeamsDeliveryError",
    "TeamsKeyDiscoveryError",
    "TeamsOutbound",
    "TeamsServiceUrlError",
    "conversation_ref_from_activity",
    "extract_card_action",
    "teams_messaging_endpoint",
    "teams_topic_id",
]
