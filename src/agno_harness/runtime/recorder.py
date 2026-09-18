"""ChunkRecorder — keep the first few raw Agno chunks of each type.

The question you ask constantly while writing a parser or a filter is "what does
an Agno ``ToolCallStarted`` chunk actually contain?". The docs will not tell you
and neither will the AG-UI events, because by then the information has already
been converted and thrown away.

So the bridge keeps a small, bounded sample: the first N chunks *per event type*,
serialized, each paired with the ``StreamState`` as it stood at that moment.
That gives you a live index of the raw shapes flowing through your agent plus the
translator state that produced them.

Deliberately process-local and never persisted — it is a development aid, it must
not grow without bound and it must not put model output in your database.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from agno.os.interfaces.agui.state import StreamState

# Chunk payloads can carry a whole model response; truncate so a sample stays a
# sample rather than a memory leak with extra steps.
_MAX_REPR = 4000

# Nested objects are expanded to this depth, which is enough to reach the fields
# you actually write filters against without following an entire object graph.
_MAX_DEPTH = 6


@dataclass(frozen=True)
class ChunkSample:
    """One recorded chunk plus the translator state at that instant."""

    event_type: str
    sequence: int
    run_id: str
    thread_id: str
    recorded_at: float
    payload: dict[str, Any]
    stream_state: dict[str, Any]


@dataclass
class ChunkRecorder:
    """Bounded per-event-type ring buffer of raw Agno chunks.

    Parameters
    ----------
    samples_per_type:
        How many chunks to keep for each event type. The first ones are the
        interesting ones — later chunks of the same type are near-identical — so
        this keeps the earliest and drops the rest.
    enabled:
        Set ``False`` to make ``record`` a no-op with no allocation.
    """

    samples_per_type: int = 3
    enabled: bool = True
    _samples: dict[str, list[ChunkSample]] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)
    _sequence: int = 0

    def record(self, chunk: Any, state: StreamState) -> None:
        if not self.enabled:
            return
        event_type = chunk_event_value(chunk) or chunk.__class__.__name__
        self._sequence += 1
        self._counts[event_type] = self._counts.get(event_type, 0) + 1

        bucket = self._samples.setdefault(event_type, [])
        if len(bucket) >= self.samples_per_type:
            return
        bucket.append(
            ChunkSample(
                event_type=event_type,
                sequence=self._sequence,
                run_id=state.run_id,
                thread_id=state.thread_id,
                recorded_at=time.time(),
                payload=serialize_chunk(chunk),
                stream_state=snapshot_stream_state(state),
            )
        )

    # ── read side ─────────────────────────────────────────────────────────

    def samples(self, event_type: str | None = None) -> dict[str, list[ChunkSample]]:
        if event_type is not None:
            return {event_type: list(self._samples.get(event_type, []))}
        return {k: list(v) for k, v in self._samples.items()}

    def counts(self) -> dict[str, int]:
        """Total chunks seen per event type, including ones not sampled."""
        return dict(self._counts)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready view for the debug endpoint."""
        return {
            "samplesPerType": self.samples_per_type,
            "totalChunks": self._sequence,
            "types": [
                {
                    "eventType": event_type,
                    "count": self._counts.get(event_type, 0),
                    "samples": [
                        {
                            "sequence": s.sequence,
                            "runId": s.run_id,
                            "threadId": s.thread_id,
                            "recordedAt": s.recorded_at,
                            "payload": s.payload,
                            "streamState": s.stream_state,
                        }
                        for s in samples
                    ],
                }
                for event_type, samples in sorted(self._samples.items())
            ],
        }

    def clear(self) -> None:
        self._samples.clear()
        self._counts.clear()
        self._sequence = 0


def snapshot_stream_state(state: StreamState) -> dict[str, Any]:
    """A JSON-safe view of Agno's translator state."""
    return {
        "threadId": state.thread_id,
        "runId": state.run_id,
        "textMessageId": state.text_message_id,
        "textMessageOpen": state.text_message_open,
        "activeToolCallIds": sorted(state.active_tool_call_ids),
        "endedToolCallIds": sorted(state.ended_tool_call_ids),
        "pendingToolCallsParentId": state.pending_tool_calls_parent_id,
        "reasoningMessageId": state.reasoning_message_id,
        "reasoningStepCount": state.reasoning_step_count,
        "runState": state.run_state,
    }


def chunk_event_value(chunk: Any) -> str:
    """An Agno chunk's event name, or ``""`` for chunks that carry none."""
    event = getattr(chunk, "event", None)
    if event is None:
        return ""
    return event.value if hasattr(event, "value") else str(event)


def serialize_chunk(chunk: Any) -> dict[str, Any]:
    """Best-effort JSON-safe dump of an Agno chunk."""
    unwrapped = _unwrap(chunk, 0)
    return unwrapped if unwrapped is not None else {"value": _truncate(chunk)}


def _unwrap(value: Any, depth: int) -> dict[str, Any] | None:
    """One level of object-to-dict, or ``None`` if there is no sensible one.

    Agno's run events are dataclasses whose most useful field -- ``tool`` on a
    ``ToolCallStarted`` -- is itself a dataclass, so this has to recurse rather
    than repr the moment it meets a non-primitive. ``asdict`` is avoided because
    it deep-copies and chokes on the odd unpicklable field.
    """
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return {k: _truncate(v, depth + 1) for k, v in dump(mode="json").items()}
        except Exception:  # noqa: BLE001 - fall through to the generic paths
            pass
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _truncate(getattr(value, f.name, None), depth + 1) for f in fields(value)}
    if hasattr(value, "__dict__") and vars(value):
        return {str(k): _truncate(v, depth + 1) for k, v in vars(value).items()}
    return None


def _truncate(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) <= _MAX_REPR:
            return value
        return value[:_MAX_REPR] + f"… (+{len(value) - _MAX_REPR} chars)"
    if isinstance(value, int | float | bool | type(None)):
        return value
    # Past this depth the payload stops being a useful sample and starts being a
    # way to blow up the debug endpoint, so anything left is repr'd flat.
    if depth >= _MAX_DEPTH:
        return repr(value)[:_MAX_REPR]
    if isinstance(value, dict):
        return {str(k): _truncate(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_truncate(v, depth + 1) for v in list(value)[:50]]
    unwrapped = _unwrap(value, depth)
    return unwrapped if unwrapped is not None else repr(value)[:_MAX_REPR]


__all__ = [
    "ChunkRecorder",
    "ChunkSample",
    "chunk_event_value",
    "serialize_chunk",
    "snapshot_stream_state",
]
