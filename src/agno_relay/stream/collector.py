from typing import Any

from ag_ui.core import EventType

from ..core.channel import OutboundMessage
from ..core.streamui.schema import CardCatalog
from ..runtime.closure import strip_stream_ui


class MessageCollector:
    """Collects streaming events, strips XML fences, and aggregates items into platform cards."""

    def __init__(self, platform: str, catalog: CardCatalog | None = None) -> None:
        self.platform = platform
        self.catalog = catalog
        self.text_chunks: list[str] = []
        self.current_block: dict[str, Any] | None = None
        self.rendered_items: list[Any] = []
        self.cards: list[dict[str, Any]] = []

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

            if name == "ui.block.start":
                self.current_block = {
                    "name": value.get("name", ""),
                    "props": value.get("props", {}),
                }
                self.rendered_items = []

            elif name == "ui.item":
                item_name = value.get("name", "")
                data = value.get("data", {})
                resolved = value.get("resolved")

                if self.catalog is not None:
                    rendered = self.catalog.render_item(self.platform, item_name, data, resolved)
                    self.rendered_items.append(rendered)

            elif name == "ui.block.end":
                if self.current_block is not None and self.catalog is not None:
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
        return OutboundMessage(
            text=clean_text,
            cards=self.cards,
            raw_text=raw_text,
        )
