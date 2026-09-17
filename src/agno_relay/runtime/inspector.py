"""RunInspector — the debug state of one run.

Everything the debug UI reads used to live on the runtime object: one recorder,
one ``_last_violations`` list, one map of stream states. That is fine for a
single run at a time and wrong the moment two runs overlap, which is the normal
case for a server — the second run's repairs overwrite the first's, and a
developer reading ``/debug/violations`` gets an answer about a run they never
asked about.

So the state moves down to where it belongs: one inspector per run, held in a
bounded registry keyed by run id. The aggregate accessors stay, but they now
mean "the most recent run" explicitly rather than "whatever was last written".
"""

from __future__ import annotations

from typing import Any

from agno.os.interfaces.agui.state import StreamState

from ..core.sequencer import ProtocolViolation
from .recorder import ChunkRecorder, snapshot_stream_state

# How many runs' debug state to keep. Chunk samples carry raw model output, so
# this is a memory bound and a disclosure bound at the same time.
DEFAULT_REMEMBERED_RUNS = 20


class RunInspector:
    """Chunk samples, protocol repairs and final translator state for one run."""

    def __init__(
        self,
        *,
        thread_id: str,
        run_id: str,
        samples_per_type: int = 3,
    ) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.recorder = ChunkRecorder(
            samples_per_type=max(samples_per_type, 0), enabled=samples_per_type > 0
        )
        self.violations: list[ProtocolViolation] = []
        self.stream_state: dict[str, Any] | None = None
        self.warnings: list[str] = []

    def record_chunk(self, chunk: Any, state: StreamState) -> None:
        self.recorder.record(chunk, state)

    def warn(self, message: str) -> None:
        """Note a degradation that did not stop the run."""
        self.warnings.append(message)

    def finish(
        self,
        *,
        violations: list[ProtocolViolation] | None = None,
        stream_state: StreamState | None = None,
    ) -> None:
        if violations is not None:
            self.violations = list(violations)
        if stream_state is not None:
            self.stream_state = snapshot_stream_state(stream_state)

    def as_dict(self) -> dict[str, Any]:
        return {
            "threadId": self.thread_id,
            "runId": self.run_id,
            "chunks": self.recorder.as_dict(),
            "streamState": self.stream_state,
            "violations": [violation_to_dict(v) for v in self.violations],
            "warnings": list(self.warnings),
        }


class InspectorRegistry:
    """The last N runs' inspectors, newest last."""

    def __init__(self, *, max_runs: int = DEFAULT_REMEMBERED_RUNS) -> None:
        self._max_runs = max(max_runs, 1)
        self._runs: dict[str, RunInspector] = {}

    def open(self, *, thread_id: str, run_id: str, samples_per_type: int) -> RunInspector:
        inspector = RunInspector(
            thread_id=thread_id, run_id=run_id, samples_per_type=samples_per_type
        )
        # Re-inserting moves an existing run to the newest position, so a
        # resumed run is not evicted for having started long ago.
        self._runs.pop(run_id, None)
        self._runs[run_id] = inspector
        while len(self._runs) > self._max_runs:
            self._runs.pop(next(iter(self._runs)))
        return inspector

    def get(self, run_id: str) -> RunInspector | None:
        return self._runs.get(run_id)

    def latest(self) -> RunInspector | None:
        if not self._runs:
            return None
        return self._runs[next(reversed(self._runs))]

    def run_ids(self) -> list[str]:
        return list(self._runs)


def violation_to_dict(violation: ProtocolViolation) -> dict[str, Any]:
    return {
        "rule": violation.rule,
        "detail": violation.detail,
        "eventType": violation.event_type,
        "repair": violation.repair,
        "index": violation.index,
    }


__all__ = [
    "DEFAULT_REMEMBERED_RUNS",
    "InspectorRegistry",
    "RunInspector",
    "violation_to_dict",
]
