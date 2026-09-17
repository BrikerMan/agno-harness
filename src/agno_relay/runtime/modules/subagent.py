"""Streaming a sub-agent's work through the parent's AG-UI run.

The usual way to delegate is to await the sub-agent inside a tool::

    result = await reviewer.arun(task, stream=False)

which makes the whole delegation a black box: the user watches a spinner until
the sub-agent is done, then the answer appears. All the reasoning and tool calls
that happened inside are lost.

This module makes the inner run visible. A tool opens a sub-stream, forwards
every chunk to the run's side channel, and the runtime interleaves that channel
with the parent's own chunks — so the sub-agent's text, reasoning and tool calls
arrive live, bracketed by ``subagent.start`` and ``subagent.end`` so a client
knows which frames were the sub-agent's.

Usage from a delegate tool::

    from agno_relay import substream

    async def delegate(agent_name: str, task: str) -> str:
        async with substream(agent_name, description="Reviewing add()", prompt=task) as emit:
            chunks = []
            async for chunk in reviewer.arun(task, stream=True, stream_events=True):
                await emit(chunk)
                chunks.append(chunk)
        return final_text(chunks)

``emit.sub_run_id`` identifies the delegation on the wire, so a tool that wants
the link in its own persisted result can return it.

Interleaving is the trick. While a tool is running, the parent's stream is
blocked awaiting the next chunk, so a queue alone would buffer everything and
deliver it in a burst after the tool returned. :func:`merge_side_channel`
therefore races the parent stream against the channel and yields whichever is
ready first.

THE BRACKET IS THE WHOLE RECORD
-------------------------------
This module used to also accumulate a transcript of everything it emitted —
text, reasoning, tool calls, cards — and save that as a ``subagent.run`` record,
because Agno persists the parent's session only and a delegation would otherwise
vanish on reload.

That was a second implementation of the frontend's renderer, living on the
server, kept in agreement with the first by a parity test. Now that a run's
frames are stored as they were sent, the boundary events are enough: replay
hands back the same frames in the same order, and the client rebuilds the panel
with the same reducer it uses live. There is no second implementation left to
disagree.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ag_ui.core import BaseEvent, CustomEvent, EventType
from agno.os.interfaces.agui.handlers import is_completion_event, process_completion
from agno.os.interfaces.agui.state import StreamState
from agno.run.agent import RunCompletedEvent

from ..module import ChunkConverter, Module
from ..scope import RunScope
from ..sidechannel import (
    ParentChunk,
    SideChannel,
    bind_channel,
    current_channel,
    merge_side_channel,
)

# Boundaries around a sub-agent's forwarded events. The events themselves stay
# ordinary AG-UI frames — these say who produced the ones in between, which is
# what lets a client group them into their own collapsible transcript.
EVENT_SUBAGENT_START = "subagent.start"
EVENT_SUBAGENT_END = "subagent.end"


def _new_sub_run_id(agent_name: str) -> str:
    return f"{agent_name}-{uuid.uuid4().hex[:12]}"


class SubAgentSignal(StrEnum):
    START = "start"
    CHUNK = "chunk"
    END = "end"


@dataclass
class SubAgentItem:
    """One item on the channel: a boundary marker or a forwarded chunk."""

    signal: SubAgentSignal
    agent_name: str
    chunk: Any = None
    # Identifies this delegation. Minted by ``substream`` rather than by the
    # runtime so the delegating tool can read it back and record the link itself.
    sub_run_id: str = ""
    # Only on START. ``description`` is the one-line "what am I doing" a client
    # can put in a collapsed header; ``prompt`` is the full task behind it.
    description: str | None = None
    prompt: str | None = None

    @property
    def bracket(self) -> int:
        """Hold the parent's chunks for as long as this delegation is open."""
        if self.signal is SubAgentSignal.START:
            return 1
        return -1 if self.signal is SubAgentSignal.END else 0


#: The sub-agent bus is the run's side channel; the name survives because a
#: delegating tool reads about ``substream``, not about queues.
SubAgentBus = SideChannel


class SubStream:
    """What a delegating tool gets from :func:`substream`.

    Call it with a chunk to forward that chunk. It also carries
    :attr:`sub_run_id`, which identifies this delegation on the wire — a tool
    that wants the link recorded in its own persisted result can return it,
    since Agno stores tool results and knows nothing about the channel.
    """

    def __init__(self, sub_run_id: str, agent_name: str, bus: SubAgentBus | None = None) -> None:
        self.sub_run_id = sub_run_id
        self.agent_name = agent_name
        self._bus = bus

    async def __call__(self, chunk: Any) -> None:
        if self._bus is None:
            return
        await self._bus.put(
            SubAgentItem(
                signal=SubAgentSignal.CHUNK,
                agent_name=self.agent_name,
                chunk=chunk,
                sub_run_id=self.sub_run_id,
            )
        )


bind_bus = bind_channel
current_bus = current_channel


@contextlib.asynccontextmanager
async def substream(
    agent_name: str,
    *,
    description: str | None = None,
    prompt: str | None = None,
) -> AsyncIterator[SubStream]:
    """Open a sub-agent stream, yielding the :class:`SubStream` to forward with.

    ``description`` is a short summary of the task — a client can show it while
    the sub-agent works, so the user reads "Reviewing the add() helper" rather
    than a bare agent name. ``prompt`` is the full task, worth keeping for the
    expanded view because a delegated prompt is usually the interesting part of
    the delegation.

    Outside a run — or when sub-agent streaming is disabled — forwarding is a
    no-op, so the same tool works unchanged in tests, scripts and non-streaming
    contexts.
    """
    sub_run_id = _new_sub_run_id(agent_name)
    bus = current_bus()
    if bus is None or not bus.enabled:
        yield SubStream(sub_run_id, agent_name)
        return

    await bus.put(
        SubAgentItem(
            signal=SubAgentSignal.START,
            agent_name=agent_name,
            sub_run_id=sub_run_id,
            description=description,
            prompt=prompt,
        )
    )
    try:
        yield SubStream(sub_run_id, agent_name, bus)
    finally:
        # Always close the bracket: a sub-agent that raises must not leave the
        # UI showing a panel that never finishes.
        await bus.put(
            SubAgentItem(
                signal=SubAgentSignal.END,
                agent_name=agent_name,
                sub_run_id=sub_run_id,
            )
        )


merge_subagent_stream = merge_side_channel


# ── per-run bookkeeping ───────────────────────────────────────────────────


class SubAgentTracker:
    """The sub-agents streaming into one parent run.

    Keyed by ``sub_run_id`` rather than by agent name. Delegating to the same
    agent twice in one run is two delegations, and keying by name let the second
    overwrite the first's :class:`StreamState` — after which both panels minted
    message ids from one counter and their text interleaved into a single
    message.

    Each delegation keeps its own ``StreamState`` so its message and tool-call
    ids are independent of the parent's.
    """

    def __init__(self, run_id: str, thread_id: str) -> None:
        self._run_id = run_id
        self._thread_id = thread_id
        self._open: dict[str, SubAgentRecord] = {}
        self._states: dict[str, StreamState] = {}
        # Parent tool calls that have started and not yet returned. When a
        # delegation opens while exactly one is in flight, that call is the
        # delegator, which is how a client links panel to card.
        self._open_tools: dict[str, str] = {}

    # ── tool-call attribution ─────────────────────────────────────────────

    def track_tool(self, event: BaseEvent) -> None:
        """Keep the set of parent tool calls that started and have not returned.

        Called before filtering, because the tool that delegates is usually a
        hidden one: its card is suppressed in favour of the sub-agent panel, and
        a dropped event still opened a call. Events produced *by* a sub-agent
        are ignored — only the parent's own calls can be the delegator.
        """
        if self._open:
            return
        etype = getattr(event, "type", None)
        if etype is EventType.TOOL_CALL_START:
            self._open_tools[_tool_call_id(event)] = getattr(event, "tool_call_name", "") or "tool"
        elif etype in (EventType.TOOL_CALL_END, EventType.TOOL_CALL_RESULT):
            self._open_tools.pop(_tool_call_id(event), None)

    def _sole_open_tool(self) -> str | None:
        """The one tool call in flight, or ``None`` when that is ambiguous.

        A delegation happens inside the tool that asked for it, so with a single
        call open the attribution is certain. With several running in parallel
        it is a guess, and a wrong link is worse for the client than no link.
        """
        if len(self._open_tools) != 1:
            return None
        return next(iter(self._open_tools))

    # ── delegation lifecycle ──────────────────────────────────────────────

    def open(self, item: SubAgentItem) -> SubAgentRecord:
        sub_run_id = item.sub_run_id or _new_sub_run_id(item.agent_name)
        record = SubAgentRecord(
            sub_run_id=sub_run_id,
            name=item.agent_name,
            description=item.description,
            prompt=item.prompt,
            tool_call_id=self._sole_open_tool(),
        )
        self._open[sub_run_id] = record
        self._states[sub_run_id] = StreamState(
            # The parent's thread, because that is the thread this is happening
            # in. It used to be the agent's name, which typed as a string and so
            # went unnoticed while putting a display label where an id belongs.
            thread_id=self._thread_id,
            run_id=f"{self._run_id}:{sub_run_id}",
        )
        return record

    def state(self, sub_run_id: str) -> StreamState | None:
        return self._states.get(sub_run_id)

    def close(self, sub_run_id: str) -> SubAgentRecord | None:
        self._states.pop(sub_run_id, None)
        record = self._open.pop(sub_run_id, None)
        if record is not None:
            record.elapsed_ms = int((time.monotonic() - record.started_at) * 1000)
        return record

    @property
    def open_ids(self) -> list[str]:
        return list(self._open)


@dataclass
class SubAgentRecord:
    """One delegation: who, why, and how long it took.

    Identity and timing only. What the sub-agent produced is in the frames
    between its two boundary events, which is the same place the client reads
    it from live.
    """

    sub_run_id: str
    name: str
    description: str | None = None
    prompt: str | None = None
    tool_call_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    elapsed_ms: int = 0
    interrupted: bool = False

    def start_value(self) -> dict[str, Any]:
        """The payload of the ``subagent.start`` boundary."""
        value: dict[str, Any] = {"name": self.name, "subRunId": self.sub_run_id}
        # Absent rather than null: the client shows the agent name when the
        # delegating tool did not describe the task.
        if self.description:
            value["description"] = self.description
        if self.prompt:
            value["prompt"] = self.prompt
        if self.tool_call_id:
            value["toolCallId"] = self.tool_call_id
        return value

    def end_value(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "subRunId": self.sub_run_id,
            "elapsedMs": self.elapsed_ms,
        }
        if self.interrupted:
            # The panel closes either way; this says whether what it shows is
            # the whole delegation or as far as it got.
            value["interrupted"] = True
        return value


def _tool_call_id(event: BaseEvent) -> str:
    return str(getattr(event, "tool_call_id", "") or "")


# ── the module ────────────────────────────────────────────────────────────


class SubAgentModule(Module):
    """Turns side-channel items into AG-UI frames, bracketed per delegation."""

    name: str = "subagent"
    namespace: str | None = "subagent."

    def __init__(self, *, stores: Any = None, enabled: bool = True) -> None:
        self.stores = stores
        self.enabled = enabled

    def _tracker(self, run: RunScope) -> SubAgentTracker:
        data = self.data(run)
        tracker = data.get("tracker")
        if tracker is None:
            tracker = SubAgentTracker(run.run_id, run.thread_id)
            data["tracker"] = tracker
        return tracker

    async def stage(self, event: BaseEvent, run: RunScope) -> AsyncIterator[BaseEvent]:
        # Before the tool filters, because the delegating tool is usually one of
        # the hidden ones and a dropped event still opened a call.
        self._tracker(run).track_tool(event)
        yield event

    def claims_item(self, item: Any) -> bool:
        return isinstance(item, SubAgentItem)

    async def stage_item(
        self, item: Any, run: RunScope, *, convert: ChunkConverter
    ) -> AsyncIterator[BaseEvent]:
        """Convert one item, keeping each delegation on its own StreamState."""
        tracker = self._tracker(run)

        if item.signal is SubAgentSignal.START:
            record = tracker.open(item)
            yield CustomEvent(
                type=EventType.CUSTOM, name=EVENT_SUBAGENT_START, value=record.start_value()
            )
            return

        state = tracker.state(item.sub_run_id)
        if state is None:
            return

        if item.signal is SubAgentSignal.CHUNK:
            run.inspector.record_chunk(item.chunk, state)
            if not is_completion_event(item.chunk):
                for event in convert(item.chunk, state):
                    yield event
            return

        # END: close whatever the sub-agent left open, but keep the run-level
        # events for the parent — a sub-agent does not finish the outer run.
        for event in process_completion(RunCompletedEvent(), state):
            if getattr(event, "type", None) in (
                EventType.RUN_FINISHED,
                EventType.STATE_SNAPSHOT,
            ):
                continue
            yield event
        closed = tracker.close(item.sub_run_id)
        if closed is not None:
            yield CustomEvent(
                type=EventType.CUSTOM, name=EVENT_SUBAGENT_END, value=closed.end_value()
            )

    async def on_run_finish(self, run: RunScope, *, error: bool) -> AsyncIterator[BaseEvent]:
        """Close any panel still open, so a failed run leaves nothing spinning.

        Normally the ``substream`` context manager closes the bracket even when
        the sub-agent raises. It cannot when the failure happens further out —
        the parent stream dying, the client vanishing — and that is precisely
        when an unclosed panel would be least noticed.
        """
        tracker = self._tracker(run)
        for sub_run_id in tracker.open_ids:
            record = tracker.close(sub_run_id)
            if record is None:
                continue
            record.interrupted = True
            yield CustomEvent(
                type=EventType.CUSTOM, name=EVENT_SUBAGENT_END, value=record.end_value()
            )


__all__ = [
    "EVENT_SUBAGENT_END",
    "EVENT_SUBAGENT_START",
    "ParentChunk",
    "SubAgentBus",
    "SubAgentItem",
    "SubAgentModule",
    "SubAgentRecord",
    "SubAgentSignal",
    "SubAgentTracker",
    "SubStream",
    "bind_bus",
    "current_bus",
    "merge_subagent_stream",
    "substream",
]
