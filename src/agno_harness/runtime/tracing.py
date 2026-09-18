"""RunTracer — write the Agno chunk to AG-UI event mapping to a file.

WHY
---
The debug panel shows what went *out*. The Chunks tab shows samples of what came
*in*. Neither answers the question you actually have when something looks wrong:
*which* Agno chunk produced *which* AG-UI events, in what order, and how long the
bridge waited between them.

This writes exactly that, one JSONL record per source, as the run happens:

    {"seq": 12, "t": 4874.2, "dt": 67.5, "source": "agno",
     "chunk": {"event": "RunContent", ...},
     "events": [{"type": "TEXT_MESSAGE_CONTENT", ...}],
     "summary": "RunContent -> TEXT_MESSAGE_CONTENT"}

Records with an empty ``events`` list are the interesting ones: a chunk arrived
and produced nothing, because a filter dropped it or the sequencer held it back.
Records whose ``source`` is not ``agno`` come from the bridge itself — pre-run
hooks, the state tracker, the sequencer's closing repairs.

``t`` is milliseconds since the run opened and ``dt`` is the gap since the
previous record, which is what makes a stalled provider or a slow post-run step
obvious: the gap sits on the record that was waiting, not spread across the run.

Reading a trace::

    tail -f traces/run-abc123.jsonl | jq -r '"\\(.t | floor)ms  \\(.summary)"'

Off by default. Writes are small and synchronous, which is fine for a
development aid and is the point — the file is complete even if the process
dies mid-run.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ag_ui.core import BaseEvent

from .recorder import chunk_event_value, serialize_chunk

#: Environment variable checked when the bridge is not given an explicit dir.
TRACE_DIR_ENV = "AGNO_HARNESS_TRACE_DIR"

# Deltas are only meaningful to a tenth of a millisecond, and full precision
# makes the file noisy to diff.
_PRECISION = 1


class RunTracer:
    """Per-run writer for the chunk-to-event mapping.

    One instance per run; not reusable and not thread-safe. Create it through
    :meth:`open`, which returns ``None`` when tracing is off so callers can keep
    a plain ``tracer is not None`` check on the hot path.
    """

    def __init__(self, path: Path, *, thread_id: str, run_id: str) -> None:
        self.path = path
        self.thread_id = thread_id
        self.run_id = run_id

        self._started = time.perf_counter()
        self._previous = self._started
        self._sequence = 0
        self._source: dict[str, Any] | None = None
        self._opened_at = self._started
        self._events: list[BaseEvent] = []
        self._counts: dict[str, int] = {}

        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="utf-8")
        self._write(
            {
                "kind": "run",
                "threadId": thread_id,
                "runId": run_id,
                "startedAt": time.time(),
            }
        )

    @classmethod
    def open(
        cls, trace_dir: str | os.PathLike[str] | None, *, thread_id: str, run_id: str
    ) -> RunTracer | None:
        """A tracer for this run, or ``None`` if tracing is disabled."""
        directory = trace_dir if trace_dir is not None else os.getenv(TRACE_DIR_ENV)
        if not directory:
            return None
        safe = _safe_name(run_id) or "run"
        return cls(Path(directory) / f"{safe}.jsonl", thread_id=thread_id, run_id=run_id)

    # ── recording ─────────────────────────────────────────────────────────

    def chunk(self, chunk: Any) -> None:
        """Open a record for a chunk that just arrived from Agno."""
        self._open(
            {
                "source": "agno",
                "chunk": {
                    "event": chunk_event_value(chunk) or type(chunk).__name__,
                    **serialize_chunk(chunk),
                },
            }
        )

    def stage(self, name: str, detail: Any = None) -> None:
        """Open a record for events the bridge produced itself."""
        source: dict[str, Any] = {"source": name}
        if detail is not None:
            source["detail"] = detail
        self._open(source)

    def event(self, event: BaseEvent) -> None:
        """Attribute one emitted AG-UI event to the open record."""
        self._events.append(event)

    def finish(self, *, violations: list[Any] | None = None) -> None:
        """Flush the last record, append a footer and close the file."""
        self._flush()
        self._write(
            {
                "kind": "summary",
                "runId": self.run_id,
                "durationMs": self._elapsed(),
                "records": self._sequence,
                "eventCounts": dict(sorted(self._counts.items())),
                "violations": [str(v) for v in violations or []],
            }
        )
        self._handle.close()

    # ── internals ─────────────────────────────────────────────────────────

    def _open(self, source: dict[str, Any]) -> None:
        """Close the previous record and start a new one, stamped *now*.

        The stamp is taken here rather than at flush time so ``t`` is when the
        chunk reached the bridge, not when the bridge finished with it. That is
        the whole point: a gap in ``dt`` then means the bridge was waiting.
        """
        self._flush()
        self._source = source
        self._opened_at = time.perf_counter()

    def _flush(self) -> None:
        """Write the open record. A source that produced no events still writes."""
        if self._source is None:
            return
        self._sequence += 1

        events = [_event_dict(e) for e in self._events]
        for event in events:
            kind = event.get("type", "?")
            self._counts[kind] = self._counts.get(kind, 0) + 1

        record = {
            "kind": "map",
            "seq": self._sequence,
            "t": round((self._opened_at - self._started) * 1000, _PRECISION),
            "dt": round((self._opened_at - self._previous) * 1000, _PRECISION),
            **self._source,
            "events": events,
            "summary": self._summary(events),
        }
        self._previous = self._opened_at
        self._source = None
        self._events = []
        self._write(record)

    def _summary(self, events: list[dict[str, Any]]) -> str:
        source = self._source or {}
        name = (
            str(source.get("chunk", {}).get("event", "?"))
            if source.get("source") == "agno"
            else str(source.get("source", "?"))
        )
        if not events:
            return f"{name} -> (nothing)"
        return f"{name} -> {', '.join(str(e.get('type', '?')) for e in events)}"

    def _elapsed(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, _PRECISION)

    def _write(self, record: dict[str, Any]) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._handle.flush()


def _event_dict(event: BaseEvent) -> dict[str, Any]:
    dump = getattr(event, "model_dump", None)
    raw = dump(mode="json", exclude_none=True) if callable(dump) else {"repr": repr(event)}
    kind = raw.get("type")
    if kind is not None:
        # "EventType.TEXT_MESSAGE_CONTENT" is noise in every single record.
        raw["type"] = str(kind).replace("EventType.", "")
    return raw


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)[:120]


__all__ = ["TRACE_DIR_ENV", "RunTracer"]
