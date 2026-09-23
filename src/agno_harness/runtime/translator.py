"""EventTranslator — Agno chunks in, well-formed AG-UI events out.

This is the half of the old bridge worth reusing on its own. It does not know
what an agent is, never calls ``arun``, and has no opinion about HTTP: you hand
it chunks from wherever you got them and it hands back events that satisfy the
AG-UI protocol.

That makes the "drive it yourself" path a first-class one rather than a thing you
could theoretically do by copying internals::

    scope = make_run_scope(thread_id="t1", run_id="r1")
    translator = EventTranslator(scope=scope)

    async for event in translator.start():
        ...
    async for chunk in agent.arun(..., session_state=my_state):
        async for event in translator.feed(chunk):
            ...                      # inspect, rewrite, persist, forward
    async for event in translator.finish():
        ...

Per chunk, in order: the run's recorder takes a sample, Agno's official handler
converts it, registered parsers add what the handler does not produce, the
tool-filter chain drops or rewrites, the StreamUI stage lifts fenced blocks
out of the text, and the sequencer enforces protocol invariants.

Only the sequencer's output is yielded. Because it repairs ordering centrally,
every stage above it can stay simple: a filter drops a tool's events without
worrying about the empty parent message Agno created to host them, and a
reasoning parser suppresses text without worrying about a stranded
``TEXT_MESSAGE_START``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import is_dataclass, replace
from typing import Any

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    EventType,
    RunErrorEvent,
    RunStartedEvent,
)
from agno.os.interfaces.agui.handlers import (
    HANDLERS,
    _normalize_event,
    is_completion_event,
    process_completion,
)
from agno.os.interfaces.agui.state import StreamState
from agno.run.agent import RunCompletedEvent, RunEvent

from ..core.protocol import WIRE_PROTOCOL_VERSION
from ..core.sequencer import EventSequencer, SequencerMode
from .hitl import describe_pause
from .module import ModuleRegistry
from .modules.subagent import ParentChunk
from .recorder import chunk_event_value
from .scope import RunScope
from .tracing import RunTracer
from .types import EventParser

EVENT_RUN_PAUSED = "run.paused"
EVENT_RUN_CANCELLED = "run.cancelled"

# Agno reports a failed or cancelled run as an ordinary chunk and then ends the
# stream normally. Nothing in the official HANDLERS map covers these, so without
# special handling a run that died would reach the client as RUN_FINISHED --
# reporting success for a run that produced nothing.
_FAILURE_EVENTS = {
    RunEvent.run_error.value: "RunError",
    RunEvent.run_cancelled.value: "RunCancelled",
    "TeamRunError": "RunError",
    "TeamRunCancelled": "RunCancelled",
}

# agno.utils.response.get_paused_content fills an empty pause with one of these
# sentences. They are not model output. An exact match is dropped before it
# becomes a TEXT_MESSAGE_CONTENT frame; a real answer that merely contains one
# of them is left alone.
_CANNED_PAUSE_CONTENT = frozenset(
    {
        "I have tools to execute, but I need confirmation, user input, or external execution.",
        "I have tools to execute, but I need confirmation or user input.",
        "I have tools to execute, but I need confirmation or external execution.",
        "I have tools to execute, but I need user input or external execution.",
        "I have tools to execute, but I need confirmation.",
        "I have tools to execute, but I need user input.",
        "I have tools to execute, but it needs external execution.",
    }
)


class AgentRunFailed(RuntimeError):
    """The agent reported a failed or cancelled run."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class EventTranslator:
    """Converts one run's chunks into an AG-UI event stream.

    Parameters
    ----------
    scope:
        The run's identity and state. The translator writes chunk samples to
        ``scope.inspector`` and the final repair list at ``finish``.
    parsers:
        Extra converters per Agno event value, run after the official handler.
    sequencer_mode:
        ``repair`` in production, ``audit`` to record what was repaired,
        ``strict`` to raise — which is what the tests use.
    modules:
        Optional capabilities hooked into the pipeline. Empty by default, so a
        translator built by hand does exactly what it is told and nothing else.
    tracer:
        Optional per-run JSONL record of the chunk-to-event mapping.
    """

    def __init__(
        self,
        *,
        scope: RunScope,
        parsers: Mapping[str, Sequence[EventParser]] | None = None,
        sequencer_mode: SequencerMode = SequencerMode.REPAIR,
        modules: ModuleRegistry | None = None,
        tracer: RunTracer | None = None,
    ) -> None:
        self.scope = scope
        self.sequencer = EventSequencer(
            thread_id=scope.thread_id, run_id=scope.run_id, mode=sequencer_mode
        )
        self.modules = modules if modules is not None else ModuleRegistry()
        self._parsers = {key: list(value) for key, value in (parsers or {}).items()}
        self._tracer = tracer
        self._completion_chunk: Any = None
        self._accumulated_text: list[str] = []

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def accumulated_text(self) -> str:
        """All streamed text chunks accumulated so far for this run."""
        return "".join(self._accumulated_text)

    @property
    def completion_chunk(self) -> Any:
        """Agno's terminal chunk, once it has arrived. ``None`` before that."""
        return self._completion_chunk

    async def start(self) -> AsyncIterator[BaseEvent]:
        """Open the run, then let the modules set themselves up."""
        self._stage("run_started")
        raw_event: dict[str, Any] = {"protocol": WIRE_PROTOCOL_VERSION}
        if self.scope.user_text:
            raw_event["user_input"] = self.scope.user_text
            raw_event["input"] = self.scope.user_text

        for out in self.sequencer.feed(
            RunStartedEvent(
                type=EventType.RUN_STARTED,
                thread_id=self.scope.thread_id,
                run_id=self.scope.run_id,
                # Announced on the first frame of every run, in a field AG-UI
                # already ignores, so a client one version behind can say so
                # instead of rendering the mismatch as nonsense. Costs nothing
                # to send and cannot be added later without a flag day.
                raw_event=raw_event,
            )
        ):
            await self.modules.observe(out, self.scope)
            yield self._traced(out)

        async for event in self.modules.on_run_start(self.scope):
            async for out in self.inject(event):
                yield out

    async def inject(self, event: BaseEvent) -> AsyncIterator[BaseEvent]:
        """Push a ready-made AG-UI event through the pipeline.

        Used for hook output and the opening state snapshot: they still go
        through the modules and the sequencer, so an injected event cannot break
        the protocol any more than a converted one can.
        """
        async for out in self._emit(event):
            yield self._traced(out)

    async def feed(self, item: Any) -> AsyncIterator[BaseEvent]:
        """Convert one item from the agent: a chunk, or a sub-agent signal.

        Raises :class:`AgentRunFailed` when the chunk reports a failed or
        cancelled run, so the caller can turn it into ``RUN_ERROR`` rather than
        a ``RUN_FINISHED`` that claims success for a run that produced nothing.
        """
        owner = self.modules.item_owner(item)
        if owner is not None:
            self._stage(f"module:{owner.name}", type(item).__name__)
            async for event in owner.stage_item(item, self.scope, convert=self.convert):
                async for out in self._emit(event):
                    yield self._traced(out)
            return

        chunk = item.chunk if isinstance(item, ParentChunk) else item
        # Before anything else, so the record's timestamp is when Agno handed
        # the chunk over rather than when we finished with it.
        if self._tracer is not None:
            self._tracer.chunk(chunk)
        self.scope.inspector.record_chunk(chunk, self.scope.stream_state)

        failure = _FAILURE_EVENTS.get(chunk_event_value(chunk))
        if failure is not None:
            raise AgentRunFailed(_failure_message(chunk, failure), failure)

        if is_completion_event(chunk):
            self._completion_chunk = chunk
            return

        for event in self.convert(chunk, self.scope.stream_state):
            async for out in self._emit(event):
                yield self._traced(out)

    async def teardown(self, *, error: bool) -> AsyncIterator[BaseEvent]:
        """Let every module close what it opened, before the terminal event.

        Always runs, on both the success and the failure path, because the
        failure path is exactly when an unclosed bracket does damage.
        """
        self._stage("module_teardown")
        async for event in self.modules.on_run_finish(self.scope, error=error):
            async for out in self._emit(event):
                yield self._traced(out)

    async def complete(self) -> AsyncIterator[BaseEvent]:
        """Close the run the way the agent's terminal chunk says to."""
        self.scope.state_tracker.sync_from_run_context(self.scope.run_context)

        async for event in self.teardown(error=False):
            yield event

        final_chunk = (
            self._completion_chunk if self._completion_chunk is not None else RunCompletedEvent()
        )
        # A paused run is a *completed* stream that is waiting for the user. The
        # tool-call events describing what it wants are produced by Agno; this
        # adds a machine-readable summary so the client knows which kind of
        # answer each one expects.
        for descriptor in describe_pause(final_chunk):
            self._stage("run_paused")
            paused = CustomEvent(type=EventType.CUSTOM, name=EVENT_RUN_PAUSED, value=descriptor)
            async for out in self._emit(paused):
                yield self._traced(out)

        # The completion chunk was stashed when it arrived; the gap between that
        # record and this one is Agno finishing its own post-run work.
        # Drop Agno's canned pause sentence before it is framed. The chunk Agno
        # already stored is left as it was; only this copy is cleared.
        final_chunk = _without_canned_pause_content(final_chunk)
        self._stage("completion", chunk_event_value(final_chunk) or None)
        for event in process_completion(final_chunk, self.scope.stream_state):
            async for out in self._emit(event):
                yield self._traced(out)

    async def fail(self, exc: BaseException) -> AsyncIterator[BaseEvent]:
        """Close the run as an error, after the modules have cleaned up."""
        async for event in self.teardown(error=True):
            yield event

        # RunErrorEvent has no thread/run fields in the protocol, so the ids
        # ride on raw_event where a debug client can still find them.
        message, code = (
            (str(exc), exc.code)
            if isinstance(exc, AgentRunFailed)
            else (f"{type(exc).__name__}: {exc}", type(exc).__name__)
        )
        # If the run was explicitly cancelled by the agent or runner, emit a CUSTOM
        # run.cancelled event before the terminal RUN_ERROR event, so the client
        # can distinguish deliberate cancellation from unexpected crashes.
        if isinstance(exc, AgentRunFailed) and exc.code == "RunCancelled":
            cancel_event = CustomEvent(
                type=EventType.CUSTOM,
                name=EVENT_RUN_CANCELLED,
                value={"reason": "agent_cancelled", "message": message},
            )
            async for out in self._emit(cancel_event):
                yield self._traced(out)

        self._stage("run_failed", message)
        for out in self.sequencer.feed(
            RunErrorEvent(
                type=EventType.RUN_ERROR,
                message=message,
                code=code,
                raw_event={"threadId": self.scope.thread_id, "runId": self.scope.run_id},
            )
        ):
            yield self._traced(out)

    def close(self) -> Iterator[BaseEvent]:
        """Flush whatever the sequencer is still holding open, and record."""
        self._stage("sequencer_finish")
        for event in self.sequencer.finish():
            yield self._traced(event)
        self.record_outcome()

    def record_outcome(self) -> None:
        """Hand the run's repairs and final state to the inspector.

        Separate from :meth:`close` because a disconnected run never flushes the
        sequencer but its debug record should still be complete.
        """
        self.scope.inspector.finish(
            violations=list(self.sequencer.violations),
            stream_state=self.scope.stream_state,
        )
        if self._tracer is not None:
            self._tracer.finish(violations=self.scope.inspector.violations)

    async def finish(self) -> AsyncIterator[BaseEvent]:
        """The happy path: complete the run, then flush."""
        async for event in self.complete():
            yield event
        for event in self.close():
            yield event

    # ── per-chunk conversion ──────────────────────────────────────────────

    def convert(self, chunk: Any, state: StreamState) -> Iterator[BaseEvent]:
        """Agno's official handler, then any parser registered for this chunk.

        Deliberately unfiltered and unstaged: this is the raw conversion, and
        what happens to it next is the modules' business.
        """
        value = chunk_event_value(chunk)

        handler = HANDLERS.get(_normalize_event(value))
        if handler is not None:
            for event in handler(chunk, state):
                yield _repair_tool_encoding(event)

        for parser in self._parsers.get(value, []):
            yield from parser(chunk, state)

    async def _emit(self, event: BaseEvent) -> AsyncIterator[BaseEvent]:
        """Modules, then the sequencer, then back to the modules to observe."""
        delta = getattr(event, "delta", None)
        if isinstance(delta, str) and delta:
            self._accumulated_text.append(delta)
        async for staged in self.modules.stage(event, self.scope):
            for out in self.sequencer.feed(staged):
                await self.modules.observe(out, self.scope)
                yield out

    # ── tracing ───────────────────────────────────────────────────────────

    def _stage(self, name: str, detail: str | None = None) -> None:
        if self._tracer is not None:
            self._tracer.stage(name, detail)

    def _traced(self, event: BaseEvent) -> BaseEvent:
        if self._tracer is not None:
            self._tracer.event(event)
        return event


def make_run_scope(
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    user_id: str | None = None,
    session_state: dict[str, Any] | None = None,
    record_chunks: int = 3,
) -> RunScope:
    """Build a scope for driving a translator by hand.

    The managed runtime builds its own from a ``RunAgentInput``; this is the
    convenience for the manual path, so that using the translator directly does
    not mean assembling four collaborators first.
    """
    thread_id = thread_id or _new_id("thread")
    run_id = run_id or _new_id("run")
    from .inspector import RunInspector
    from .state import StateTracker

    stream_state = StreamState(thread_id=thread_id, run_id=run_id)
    tracker = StateTracker(session_state)
    tracker.bind(stream_state)
    return RunScope(
        thread_id=thread_id,
        run_id=run_id,
        user_id=user_id,
        stream_state=stream_state,
        state_tracker=tracker,
        inspector=RunInspector(thread_id=thread_id, run_id=run_id, samples_per_type=record_chunks),
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _without_canned_pause_content(chunk: Any) -> Any:
    """Return ``chunk`` with Agno's canned pause sentence removed.

    The original object is not modified, so a run Agno already stored keeps
    the sentence it wrote. Only the copy handed to ``process_completion`` is
    cleared, and only when ``content`` is exactly one of the seven sentences.
    """
    content = getattr(chunk, "content", None)
    if not isinstance(content, str) or content not in _CANNED_PAUSE_CONTENT:
        return chunk
    if is_dataclass(chunk) and not isinstance(chunk, type):
        return replace(chunk, content=None)
    return chunk


def _failure_message(chunk: Any, code: str) -> str:
    for attribute in ("content", "reason", "error_type"):
        value = getattr(chunk, attribute, None)
        if value:
            return str(value)
    return f"The run ended with {code}."


def _repair_ascii_json(text: str, *, unwrap_string: bool) -> str:
    """Turn Agno's ``ensure_ascii=True`` dumps back into readable Unicode.

    Agno's AG-UI handler json-dumps tool payloads with ASCII escapes, so CJK
    arrives as ``\\uXXXX`` inside an already-encoded string. The SSE encoder
    then dumps with ``ensure_ascii=False``, which does not decode those
    inner escapes — the client sees the ``\\u`` sequences as text.

    ``unwrap_string`` is for ``TOOL_CALL_RESULT``: a whole JSON string
    literal is Agno wrapping a plain-text result, so we peel one layer.
    ``TOOL_CALL_ARGS`` deltas must not unwrap strings — a fragment like
    ``"Tokyo"`` is valid JSON but is not the whole args object.
    """
    stripped = text.strip()
    if not stripped:
        return text
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError, TypeError):
        return text
    if isinstance(parsed, str):
        if not unwrap_string:
            return text
        inner = parsed.strip()
        if inner[:1] in "{[":
            try:
                obj = json.loads(inner)
            except (json.JSONDecodeError, ValueError, TypeError):
                return parsed
            if isinstance(obj, (dict, list)):
                return json.dumps(obj, ensure_ascii=False)
        return parsed
    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, ensure_ascii=False)
    return text


def _repair_tool_encoding(event: BaseEvent) -> BaseEvent:
    """Undo Agno's ASCII json.dumps on tool args and results."""
    etype = getattr(event, "type", None)
    if etype is EventType.TOOL_CALL_RESULT:
        content = getattr(event, "content", None)
        if not isinstance(content, str):
            return event
        repaired = _repair_ascii_json(content, unwrap_string=True)
        return event if repaired == content else event.model_copy(update={"content": repaired})
    if etype is EventType.TOOL_CALL_ARGS:
        delta = getattr(event, "delta", None)
        if not isinstance(delta, str):
            return event
        repaired = _repair_ascii_json(delta, unwrap_string=False)
        return event if repaired == delta else event.model_copy(update={"delta": repaired})
    return event


__all__ = [
    "EVENT_RUN_CANCELLED",
    "EVENT_RUN_PAUSED",
    "AgentRunFailed",
    "EventTranslator",
    "make_run_scope",
]
