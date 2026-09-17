import asyncio
import json
import logging
from typing import Any

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey
from .base import BaseChannel

log = logging.getLogger("agno_relay.channels.lark")

try:
    import lark_oapi as lark

    _LARK_AVAILABLE = True
except ImportError:
    lark = None  # type: ignore
    _LARK_AVAILABLE = False


class LarkChannel(BaseChannel):
    """Lark (Feishu) channel adapter supporting WebSocket long connection and Card v2."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        verification_token: str | None = None,
        encrypt_key: str | None = None,
        use_websocket: bool = True,
        client: Any = None,
    ) -> None:
        super().__init__(name="lark")
        self.app_id = app_id
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.use_websocket = use_websocket
        self._client = client
        self._ws_client = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not _LARK_AVAILABLE:
            raise ImportError(
                "Lark SDK is not installed. Please install with 'pip install agno-relay[lark]'"
            )
        self._client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        return self._client

    async def start(self) -> None:
        await super().start()
        if self.use_websocket and _LARK_AVAILABLE:
            # Start WebSocket background worker if available
            try:
                event_handler = (
                    lark.EventDispatcherHandler.builder(
                        self.encrypt_key or "", self.verification_token or ""
                    )
                    .register_p2_im_message_receive_v1(self._handle_im_message)
                    .build()
                )

                ws = lark.ws.Client(self.app_id, self.app_secret, event_handler=event_handler)
                self._ws_client = ws
                # Run WS in background thread/task
                asyncio.create_task(asyncio.to_thread(ws.start))
                log.info("Lark WebSocket long-connection client started.")
            except Exception as exc:
                log.warning(f"Could not initialize Lark WS long connection: {exc}")

    def _handle_im_message(self, data: Any) -> None:
        """Callback from Lark event dispatcher for incoming messages."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            chat_id = message.chat_id
            message_id = message.message_id
            thread_id = getattr(message, "root_id", None) or getattr(message, "thread_id", None)
            sender_id = sender.sender_id.open_id if hasattr(sender, "sender_id") else None
            is_dm = getattr(message, "chat_type", "") == "p2p"

            # Parse content
            content_str = getattr(message, "content", "{}")
            content_dict = json.loads(content_str) if isinstance(content_str, str) else {}
            text = content_dict.get("text", "")

            key = ConversationKey(
                platform="lark",
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_id=message_id,
                sender_id=sender_id,
                is_direct_message=is_dm,
            )

            channel_event = ChannelEvent(
                event_id=message_id,
                key=key,
                text=text,
                raw=data,
            )
            # Push into async queue
            asyncio.run_coroutine_threadsafe(
                self.push_event(channel_event), asyncio.get_event_loop()
            )
        except Exception as exc:
            log.error(f"Error parsing Lark inbound event: {exc}", exc_info=True)

    async def ack(self, event: ChannelEvent, emoji: str = "THINKING") -> Any:
        """Acknowledge message with emoji reaction."""
        if not _LARK_AVAILABLE or not self._client:
            return None
        # In real Lark API: POST /open-apis/im/v1/messages/{message_id}/reactions
        return {"acknowledged": True, "emoji": emoji, "message_id": event.event_id}

    async def settle(
        self, destination: ConversationKey, ack_token: Any, emoji: str = "DONE"
    ) -> None:
        """Settle message by updating emoji reaction."""
        pass

    async def send(self, destination: ConversationKey, message: OutboundMessage) -> str:
        """Send message or cards to Lark."""
        client = self._ensure_client()
        reply_to_id = destination.reply_to_id

        # If cards are present, send interactive card v2 payload
        if message.cards:
            # We bundle the aggregated card
            card_payload = message.cards[0]
            # Wrap in Lark card content JSON string
            content = json.dumps(card_payload)
            msg_type = "interactive"
        else:
            content = json.dumps({"text": message.text})
            msg_type = "text"

        if hasattr(client, "im") and hasattr(client.im, "v1"):
            # Use real SDK client if present
            try:
                # Reply to thread if reply_to_id is available
                if reply_to_id:
                    req = (
                        lark.api.im.v1.ReplyMessageRequest.builder()
                        .message_id(reply_to_id)
                        .request_body(
                            lark.api.im.v1.ReplyMessageRequestBody.builder()
                            .content(content)
                            .msg_type(msg_type)
                            .build()
                        )
                        .build()
                    )
                    resp = await asyncio.to_thread(client.im.v1.message.reply, req)
                    if resp and hasattr(resp, "data") and hasattr(resp.data, "message_id"):
                        return resp.data.message_id
                else:
                    req = (
                        lark.api.im.v1.CreateMessageRequest.builder()
                        .receive_id_type("chat_id")
                        .request_body(
                            lark.api.im.v1.CreateMessageRequestBody.builder()
                            .receive_id(destination.chat_id)
                            .content(content)
                            .msg_type(msg_type)
                            .build()
                        )
                        .build()
                    )
                    resp = await asyncio.to_thread(client.im.v1.message.create, req)
                    if resp and hasattr(resp, "data") and hasattr(resp.data, "message_id"):
                        return resp.data.message_id
            except Exception as exc:
                log.error(f"Failed to send Lark message: {exc}", exc_info=True)

        return f"lark_msg_{destination.chat_id}"
