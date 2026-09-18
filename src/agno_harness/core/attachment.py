"""Inbound attachment models and pluggable business processor interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .channel import ChannelEvent


class InboundAttachment(BaseModel):
    """Normalized incoming attachment from chat platforms (Teams, Lark, Web)."""

    id: str | None = None
    name: str = ""
    content_type: str | None = None
    url: str | None = None
    size: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class AttachmentProcessor(Protocol):
    """Pluggable business processor for incoming attachments.

    Allows downstream hosts to implement custom file downloads, OCR,
    DocMind parsing, S3/OSS uploads, or vector indexing.
    """

    async def process(
        self,
        attachments: list[InboundAttachment],
        event: ChannelEvent,
    ) -> str | None:
        """Process inbound attachments and optionally return context to append to the agent prompt."""
        ...
