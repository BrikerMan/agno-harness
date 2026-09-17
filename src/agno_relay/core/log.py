"""The event log: two protocols, because durability and liveness are two things.

A run's frames are wanted for two unrelated reasons. One is history — the user
reloads tomorrow and expects to see what happened. The other is resumption — the
user's train goes into a tunnel and the run should still be there, still moving,
when the connection comes back.

Those want different storage. History wants durability and does not care about
latency. Resumption wants a blocking read that wakes on the next write and does
not care whether the data survives a restart. A single protocol would force
every implementation to pretend it does both, and a SQL backend pretending to
tail would be a polling loop nobody wants to own.

So there are two, and a backend implements whichever it is actually good at:

============================  ===============  ===============
Implementation                RunEventLog      RunEventStream
============================  ===============  ===============
``InMemoryRunEventLog``       yes              yes
``SQLRunEventLog``            yes              no
``RedisRunEventLog``          yes              yes
============================  ===============  ===============

This produces three honest configurations rather than one aspirational one:

*No log.* Today's behaviour. The run is bound to the connection; if the
connection drops the run stops. A legitimate choice, not a broken one.

*Log only.* The run finishes in the background and is stored. A reload shows
what has been written so far, but cannot follow a run in progress. This is the
"no Redis means history only" case, stated plainly instead of half-working.

*Log and stream.* Reconnect resumes from an offset, mid-sentence.

HOT LOG VS HISTORY ARCHIVE
--------------------------
The live log stores what the client was sent, so reconnect is re-sending rather
than re-deriving. That log is a hot layer (Redis, or an in-process stand-in):
token-level deltas, a blocking tail, a TTL. SQL is the wrong place for it.

Finished runs are compacted into a history archive — consecutive content deltas
folded together — so a reload can still reduce the same sequence the live path
sent, without keeping every token on disk.

Frames carry ``kind``: ``delta`` for a frame as it was sent, ``snapshot`` for a
compacted stand-in. Reading is defined as "the last snapshot plus everything
after it".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class RunStatus(StrEnum):
    """Where a run is. ``PAUSED`` is not a synonym for stopped.

    A run waiting on a human is neither running nor finished, and a client that
    only asks about the first two loses it on reload while Agno's side stays
    locked waiting for the answer that will now never come.
    """

    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    ERROR = "error"
    ABORTED = "aborted"


class FrameKind(StrEnum):
    DELTA = "delta"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class Frame:
    """One stored frame: an AG-UI event plus where it sits in the run."""

    offset: str
    event: Mapping[str, Any]
    kind: FrameKind = FrameKind.DELTA


@dataclass
class RunRecord:
    """What is known about a run without reading any of its frames."""

    run_id: str
    thread_id: str
    status: RunStatus = RunStatus.RUNNING
    user_id: str | None = None
    started_at: float = 0.0
    updated_at: float = 0.0
    #: Set when the log could not keep up or was unavailable. The run continued
    #: — a recording failure must never kill a stream somebody is watching — but
    #: what is stored is incomplete and resuming from it would lie.
    unrecordable: bool = False
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in (RunStatus.RUNNING, RunStatus.PAUSED)

    @property
    def is_producing(self) -> bool:
        """Still writing frames. PAUSED stays findable (`is_open`) but the stream is over."""
        return self.status is RunStatus.RUNNING


@runtime_checkable
class RunEventLog(Protocol):
    """Durable record of a run's frames. Required for any background run."""

    async def append(self, run_id: str, frames: Sequence[Mapping[str, Any]]) -> str:
        """Store frames in order and return the offset of the last one."""
        ...

    async def read(self, run_id: str, *, after: str | None = None) -> list[Frame]:
        """Frames after ``after``, or the whole run when it is ``None``."""
        ...

    async def start_run(
        self, run_id: str, thread_id: str, *, user_id: str | None = None, **meta: Any
    ) -> RunRecord:
        """Open a run, or return the existing record if it is already open."""
        ...

    async def set_status(self, run_id: str, status: RunStatus, **meta: Any) -> None: ...

    async def get_run(self, run_id: str) -> RunRecord | None: ...

    async def list_runs(self, thread_id: str, *, user_id: str | None = None) -> list[RunRecord]: ...

    async def heartbeat(self, run_id: str) -> None:
        """Say the process driving this run is still alive.

        Without it a run whose process was killed stays ``running`` forever and
        a client waits for frames that will never come.
        """
        ...

    async def delete_thread(self, thread_id: str) -> int: ...


@runtime_checkable
class RunEventStream(Protocol):
    """Live tail of a run in progress. Optional; only some backends can do it."""

    def tail(self, run_id: str, *, after: str | None = None) -> AsyncIterator[Frame]:
        """Yield frames as they are appended, starting after ``after``.

        Ends when the run stops producing frames (finished, paused, error,
        aborted). PAUSED is still findable on ``/active``, but nothing more
        will be written until a new resume run. Implementations block rather
        than poll — the point of this protocol is not having to poll.
        """
        ...


class CompactionPolicy(Protocol):
    """Turns a finished run's frames into fewer frames.

    Reserved rather than used. The read rule that makes it possible — snapshots
    win over deltas — is already implemented, so enabling compaction later is a
    behaviour change and not a migration.
    """

    def compact(self, frames: Sequence[Frame]) -> Sequence[Mapping[str, Any]]: ...


class NoCompaction:
    """Keep everything. The default, and what every log does today."""

    def compact(self, frames: Sequence[Frame]) -> Sequence[Mapping[str, Any]]:
        return [frame.event for frame in frames]


_MERGEABLE = frozenset({"TEXT_MESSAGE_CONTENT", "REASONING_MESSAGE_CONTENT", "TOOL_CALL_ARGS"})


def coalesce_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fold consecutive content deltas of the same message into one event.

    Live streaming wants a row per token so a reconnecting client can resume
    mid-sentence. A finished run does not: the reducer concatenates ``delta``
    fields, so one CONTENT event per message is byte-equivalent after reduce
    and orders of magnitude smaller on disk.
    """
    merged: list[dict[str, Any]] = []
    for event in events:
        current = dict(event)
        kind = current.get("type")
        if (
            merged
            and kind in _MERGEABLE
            and merged[-1].get("type") == kind
            and merged[-1].get("messageId") == current.get("messageId")
            and merged[-1].get("toolCallId") == current.get("toolCallId")
        ):
            previous = merged[-1]
            field = "delta" if "delta" in previous or "delta" in current else "args"
            previous[field] = str(previous.get(field) or "") + str(current.get(field) or "")
            continue
        merged.append(current)
    return merged


def select_frames(frames: Sequence[Frame]) -> list[Frame]:
    """Apply the read rule: the last snapshot, plus everything after it.

    "Everything after" matters even though nothing writes snapshots yet: a run
    compacted while still going would otherwise have its remaining output
    swallowed by the snapshot that was meant to summarise only the part before
    it. With no snapshots present this returns the deltas unchanged, which is
    every read today.
    """
    for index in range(len(frames) - 1, -1, -1):
        if frames[index].kind is FrameKind.SNAPSHOT:
            return list(frames[index:])
    return list(frames)


__all__ = [
    "CompactionPolicy",
    "Frame",
    "FrameKind",
    "NoCompaction",
    "RunEventLog",
    "RunEventStream",
    "RunRecord",
    "RunStatus",
    "coalesce_events",
    "select_frames",
]
