"""DebugTap — capture the event stream on its way to the client.

The frontend debug panel needs more than the events themselves: it wants arrival
timing, a stable sequence number, and the protocol repairs the sequencer applied.
The tap sits between the bridge and the encoder and records all of that without
altering a single frame — whatever the client would have received, it still
receives, in the same order.

Enabled per request (``POST /agui?debug=1``) so production streams pay nothing.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ag_ui.core import BaseEvent, CustomEvent, EventType

from .sequencer import ProtocolViolation

EVENT_DEBUG_SUMMARY = "debug.summary"


@dataclass
class CapturedFrame:
    sequence: int
    event_type: str
    offset_ms: float
    payload: dict[str, Any]


@dataclass
class DebugTap:
    """Records frames as they stream past, and summarizes the run at the end."""

    started_at: float = field(default_factory=time.perf_counter)
    frames: list[CapturedFrame] = field(default_factory=list)
    first_content_ms: float | None = None
    max_frames: int = 5000

    def observe(self, event: BaseEvent) -> None:
        offset_ms = (time.perf_counter() - self.started_at) * 1000
        event_type = _type_name(event)
        if (
            self.first_content_ms is None
            and getattr(event, "type", None) is EventType.TEXT_MESSAGE_CONTENT
        ):
            self.first_content_ms = offset_ms
        if len(self.frames) >= self.max_frames:
            return
        self.frames.append(
            CapturedFrame(
                sequence=len(self.frames) + 1,
                event_type=event_type,
                offset_ms=round(offset_ms, 3),
                payload=_dump(event),
            )
        )

    def counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for frame in self.frames:
            counts[frame.event_type] = counts.get(frame.event_type, 0) + 1
        return counts

    def summary(self, violations: list[ProtocolViolation] | None = None) -> dict[str, Any]:
        total_ms = (time.perf_counter() - self.started_at) * 1000
        return {
            "frameCount": len(self.frames),
            "countsByType": self.counts_by_type(),
            "timeToFirstContentMs": (
                round(self.first_content_ms, 3) if self.first_content_ms is not None else None
            ),
            "totalMs": round(total_ms, 3),
            "violations": [
                {
                    "rule": v.rule,
                    "detail": v.detail,
                    "eventType": v.event_type,
                    "repair": v.repair,
                    "index": v.index,
                }
                for v in (violations or [])
            ],
        }

    def summary_event(self, violations: list[ProtocolViolation] | None = None) -> CustomEvent:
        """A trailing ``CUSTOM`` frame carrying the run's debug summary."""
        return CustomEvent(
            type=EventType.CUSTOM,
            name=EVENT_DEBUG_SUMMARY,
            value=self.summary(violations),
        )


async def tap_stream(events: AsyncIterator[BaseEvent], tap: DebugTap) -> AsyncIterator[BaseEvent]:
    """Pass events through unchanged while the tap records them."""
    async for event in events:
        tap.observe(event)
        yield event


def _type_name(event: BaseEvent) -> str:
    etype = getattr(event, "type", None)
    return getattr(etype, "value", None) or str(etype)


def _dump(event: BaseEvent) -> dict[str, Any]:
    dump = getattr(event, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json", exclude_none=True)
        except Exception:  # noqa: BLE001
            pass
    return {"repr": repr(event)}


__all__ = ["EVENT_DEBUG_SUMMARY", "CapturedFrame", "DebugTap", "tap_stream"]
