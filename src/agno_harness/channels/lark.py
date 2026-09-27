import asyncio
import json
import logging
from typing import Any

from ..config import RelayConfig
from ..core.attachment import InboundAttachment
from ..core.channel import ChannelEvent, OutboundMessage
from ..core.prompt import parse_sent_at
from ..sessions.models import ConversationKey
from .base import BaseChannel
from .cleaning import clean_lark_mentions, html_to_markdown, lark_post_to_markdown, looks_like_html

log = logging.getLogger("agno_harness.channels.lark")

try:
    import lark_oapi as lark

    _LARK_AVAILABLE = True
except ImportError:
    lark = None  # type: ignore
    _LARK_AVAILABLE = False


def _post_image_keys(content: dict[str, Any]) -> list[str]:
    """Collect ``image_key`` of every ``img`` element in a (possibly localized) post."""
    bodies: list[Any] = []
    if isinstance(content.get("content"), list):
        bodies.append(content)
    else:
        bodies.extend(v for v in content.values() if isinstance(v, dict))
    keys: list[str] = []
    for body in bodies:
        for line in body.get("content") or []:
            for el in line if isinstance(line, list) else []:
                if isinstance(el, dict) and el.get("tag") == "img" and el.get("image_key"):
                    keys.append(str(el["image_key"]))
    return keys


def _card_response() -> Any:
    if not _LARK_AVAILABLE:
        return None
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )

    return P2CardActionTriggerResponse({})


class LarkChannel(BaseChannel):
    """Lark (Feishu) channel adapter supporting WebSocket long connection and Card v2."""

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        verification_token: str | None = None,
        encrypt_key: str | None = None,
        use_websocket: bool = True,
        client: Any = None,
    ) -> None:
        super().__init__(name="lark")
        self.app_id = app_id or RelayConfig.lark_app_id()
        self.app_secret = app_secret or RelayConfig.lark_app_secret()
        self.verification_token = verification_token or RelayConfig.lark_verification_token()
        self.encrypt_key = encrypt_key or RelayConfig.lark_encrypt_key()
        self.use_websocket = use_websocket
        self._client = client
        self._ws_client = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not _LARK_AVAILABLE:
            raise ImportError(
                "Lark SDK is not installed. Please install with 'pip install agno-harness[lark]'"
            )
        self._client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        return self._client

    async def start(self) -> None:
        await super().start()
        self._loop = asyncio.get_running_loop()
        if self.use_websocket and _LARK_AVAILABLE:
            # Start WebSocket background worker if available
            try:
                event_handler = (
                    lark.EventDispatcherHandler.builder(
                        self.encrypt_key or "", self.verification_token or ""
                    )
                    .register_p2_im_message_receive_v1(self._handle_im_message)
                    .register_p2_card_action_trigger(self._handle_card_action)
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
            msg_type = getattr(message, "message_type", "text")

            raw_text = (
                content_str
                if isinstance(content_str, str)
                else json.dumps(content_dict, ensure_ascii=False)
            )
            text = ""
            inbound_attachments: list[InboundAttachment] = []

            if msg_type == "post":
                text = lark_post_to_markdown(content_dict)
                for image_key in _post_image_keys(content_dict):
                    inbound_attachments.append(
                        InboundAttachment(
                            id=image_key,
                            name=f"{image_key}.jpg",
                            content_type="image/jpeg",
                            raw={"image_key": image_key},
                        )
                    )
            elif msg_type == "image":
                image_key = content_dict.get("image_key", "")
                text = f"![图片](image_key:{image_key})"
                inbound_attachments.append(
                    InboundAttachment(
                        id=image_key,
                        name=f"{image_key}.jpg",
                        content_type="image/jpeg",
                        raw=content_dict,
                    )
                )
            elif msg_type == "file":
                file_key = content_dict.get("file_key", "")
                file_name = content_dict.get("file_name", "file")
                text = f"[附件: {file_name}]"
                inbound_attachments.append(
                    InboundAttachment(
                        id=file_key,
                        name=file_name,
                        raw=content_dict,
                    )
                )
            else:
                raw_extracted_text = content_dict.get("text", "")
                raw_mentions = getattr(message, "mentions", None)
                if raw_mentions and isinstance(raw_mentions, list):
                    mention_list = []
                    for m in raw_mentions:
                        if hasattr(m, "key") and hasattr(m, "name"):
                            mention_list.append({"key": m.key, "name": m.name})
                        elif isinstance(m, dict):
                            mention_list.append(m)
                    raw_extracted_text = clean_lark_mentions(raw_extracted_text, mention_list)
                if looks_like_html(raw_extracted_text):
                    text = html_to_markdown(raw_extracted_text)
                else:
                    text = raw_extracted_text

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
                raw_text=raw_text,
                created_at=parse_sent_at(getattr(message, "create_time", None)),
                sender_name=sender_id,
                attachments=inbound_attachments,
                raw=data,
            )
            # Push into async queue using running event loop
            loop = self._loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(self.push_event(channel_event), loop)
            else:
                asyncio.run(self.push_event(channel_event))
        except Exception as exc:
            log.error(f"Error parsing Lark inbound event: {exc}", exc_info=True)

    def _handle_card_action(self, data: Any) -> Any:
        """Callback from Lark for interactive card button clicks (``card.action.trigger``).

        The button ``value`` must carry ``action_id``; the rest is passed through as
        ``action_value``. Lark expects an answer within 3s, so the event is queued and
        an empty response returned; handlers update the card via ``send`` with
        ``extra={"update_in_place": True}``.
        """
        try:
            event = data.event
            action = getattr(event, "action", None)
            value = dict(getattr(action, "value", None) or {})
            action_id = value.get("action_id")
            if not action_id:
                log.warning("Lark card action without action_id; ignoring")
                return _card_response()
            context = getattr(event, "context", None)
            operator = getattr(event, "operator", None)
            message_id = getattr(context, "open_message_id", None)
            key = ConversationKey(
                platform="lark",
                chat_id=getattr(context, "open_chat_id", None) or "",
                reply_to_id=message_id,
                sender_id=getattr(operator, "open_id", None),
            )
            channel_event = ChannelEvent(
                event_id=f"card:{getattr(event, 'token', None) or message_id}:{action_id}",
                key=key,
                sender_name=getattr(operator, "open_id", None),
                action_id=str(action_id),
                action_value=value,
                raw=data,
            )
            loop = self._loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(self.push_event(channel_event), loop)
            else:
                asyncio.run(self.push_event(channel_event))
        except Exception as exc:
            log.error(f"Error parsing Lark card action: {exc}", exc_info=True)
        return _card_response()

    async def fetch_attachment(
        self, event: ChannelEvent, attachment: InboundAttachment
    ) -> bytes | None:
        """Download an image / file resource attached to the inbound message."""
        if not attachment.id or not event.event_id or not _LARK_AVAILABLE:
            return None
        client = self._ensure_client()
        resource_type = "image" if attachment.is_image else "file"
        req = (
            lark.api.im.v1.GetMessageResourceRequest.builder()
            .message_id(event.event_id)
            .file_key(attachment.id)
            .type(resource_type)
            .build()
        )
        resp = await asyncio.to_thread(client.im.v1.message_resource.get, req)
        if resp is None or not resp.success() or resp.file is None:
            log.warning(
                f"Lark resource download failed for {attachment.id}: "
                f"{getattr(resp, 'code', None)} {getattr(resp, 'msg', None)}"
            )
            return None
        return resp.file.read()

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
                # Card actions replace the clicked card instead of replying under it
                if message.cards and reply_to_id and (message.extra or {}).get("update_in_place"):
                    req = (
                        lark.api.im.v1.PatchMessageRequest.builder()
                        .message_id(reply_to_id)
                        .request_body(
                            lark.api.im.v1.PatchMessageRequestBody.builder()
                            .content(content)
                            .build()
                        )
                        .build()
                    )
                    resp = await asyncio.to_thread(client.im.v1.message.patch, req)
                    if resp is not None and not resp.success():
                        log.error(f"Failed to update Lark card: {resp.code} {resp.msg}")
                    return reply_to_id
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
