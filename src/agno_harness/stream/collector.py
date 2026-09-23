import json
from typing import Any

from ag_ui.core import EventType

from ..core.channel import OutboundMessage
from ..core.streamui.schema import CardCatalog
from ..runtime.closure import strip_stream_ui


def _text_body_fragment(
    platform: str,
    text: str,
    *,
    font: str = "",
    code: bool = False,
) -> Any:
    """Turn a text-body card's raw text into one platform fragment."""
    if platform == "teams":
        if code:
            return {"type": "CodeBlock", "codeSnippet": text, "language": "PlainText"}
        block: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": True}
        if font:
            block["fontType"] = font
        return block
    if platform == "lark":
        return {"tag": "div", "text": {"tag": "lark_md", "content": text}}
    return text


def _render_hitl_card(
    platform: str,
    descriptor: dict[str, Any],
    session_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Render a native interactive confirmation card for paused Agent runs on Teams or Lark."""
    tool_name = descriptor.get("toolName") or "unspecified_tool"
    tool_call_id = descriptor.get("toolCallId") or ""
    pause_type = descriptor.get("pauseType") or "confirmation"
    tool_args = descriptor.get("toolArgs") or {}

    base_action_data = {
        "action_id": "agno.hitl.resume",
        "tool_call_id": tool_call_id,
        "pause_type": pause_type,
        "session_id": session_id,
        "meta": meta or {},
    }

    if platform == "teams":
        facts = [{"title": str(k), "value": str(v)} for k, v in tool_args.items()]
        return {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "body": [
                {
                    "type": "TextBlock",
                    "text": f"⚠️ 人工审批确认: `{tool_name}`",
                    "weight": "Bolder",
                    "size": "Medium",
                    "color": "Warning",
                },
                {
                    "type": "TextBlock",
                    "text": "智能体请求执行受限高风险操作，请确认是否允许：",
                    "wrap": True,
                },
                {"type": "FactSet", "facts": facts or [{"title": "参数", "value": "无"}]},
            ],
            "actions": [
                {
                    "type": "Action.Submit",
                    "title": "✅ 允许执行 (Approve)",
                    "data": {
                        **base_action_data,
                        "accepted": True,
                    },
                },
                {
                    "type": "Action.Submit",
                    "title": "❌ 拒绝 (Deny)",
                    "data": {
                        **base_action_data,
                        "accepted": False,
                    },
                },
            ],
        }

    if platform == "lark":
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {"tag": "plain_text", "content": f"⚠️ 审批等待: {tool_name}"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"智能体请求执行工具 **`{tool_name}`**，请确认：\n```json\n{json.dumps(tool_args, ensure_ascii=False, indent=2)}\n```",
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 允许执行"},
                            "type": "primary",
                            "value": {
                                **base_action_data,
                                "accepted": True,
                            },
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "❌ 拒绝执行"},
                            "type": "danger",
                            "value": {
                                **base_action_data,
                                "accepted": False,
                            },
                        },
                    ],
                },
            ],
        }

    return None


class MessageCollector:
    """Collects streaming events, strips XML fences, and aggregates items into platform cards."""

    def __init__(
        self,
        platform: str,
        catalog: CardCatalog | None = None,
        session_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.platform = platform
        self.catalog = catalog
        self.session_id = session_id
        self.meta = meta or {}
        self.text_chunks: list[str] = []
        self.current_block: dict[str, Any] | None = None
        self.rendered_items: list[Any] = []
        self.cards: list[dict[str, Any]] = []
        self.paused_descriptors: list[dict[str, Any]] = []

    def feed(self, event: Any) -> None:
        """Feed a single AG-UI event into the collector."""
        event_type = getattr(event, "type", None)

        if event_type is EventType.TEXT_MESSAGE_CONTENT or getattr(event_type, "name", "") in (
            "TEXT_DELTA",
            "TEXT_MESSAGE_CONTENT",
        ):
            delta = getattr(event, "delta", "")
            if delta:
                self.text_chunks.append(delta)

        elif event_type is EventType.CUSTOM or getattr(event_type, "name", "") == "CUSTOM":
            name = getattr(event, "name", "")
            value = getattr(event, "value", {}) or {}

            if name in ("run.paused", "EVENT_RUN_PAUSED") and isinstance(value, dict):
                self.paused_descriptors.append(value)

            elif name == "ui.block.start":
                self.current_block = {
                    "name": value.get("name", ""),
                    "props": value.get("props", {}),
                }
                self.rendered_items = []

            elif name == "ui.text":
                delta = value.get("delta") if isinstance(value, dict) else ""
                if self.current_block is not None and isinstance(delta, str) and delta:
                    self.current_block["text"] = self.current_block.get("text", "") + delta

            elif name == "ui.item":
                item_name = value.get("name", "")
                data = value.get("data", {})
                resolved = value.get("resolved")

                if self.catalog is not None:
                    rendered = self.catalog.render_item(self.platform, item_name, data, resolved)
                    if self.current_block is not None:
                        self.rendered_items.append(rendered)
                    else:
                        if rendered is not None:
                            self.cards.append(rendered)

            elif name == "ui.block.end":
                if self.current_block is not None and self.catalog is not None:
                    text = str(self.current_block.get("text") or "").strip()
                    if text:
                        schema = self.catalog.block_schema(
                            str(self.current_block.get("name") or "")
                        )
                        self.rendered_items.append(
                            _text_body_fragment(
                                self.platform,
                                text,
                                font=str(getattr(schema, "teams_text_font", "") or ""),
                                code=bool(getattr(schema, "teams_code_block", False)),
                            )
                        )
                    block_name = self.current_block["name"]
                    props = self.current_block["props"]
                    card = self.catalog.render_block(
                        self.platform, block_name, props, self.rendered_items
                    )
                    if card:
                        self.cards.append(card)
                self.current_block = None
                self.rendered_items = []

    def finalize(self) -> OutboundMessage:
        """Finalize and return the consolidated OutboundMessage."""
        raw_text = "".join(self.text_chunks)
        clean_text = strip_stream_ui(raw_text).strip()

        # Generate platform-native HITL confirmation cards if the run paused
        for desc in self.paused_descriptors:
            hitl_card = _render_hitl_card(
                self.platform, desc, session_id=self.session_id, meta=self.meta
            )
            if hitl_card is not None:
                self.cards.append(hitl_card)

        return OutboundMessage(
            text=clean_text,
            cards=self.cards,
            raw_text=raw_text,
        )
