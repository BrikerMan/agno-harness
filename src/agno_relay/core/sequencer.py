"""EventSequencer — the protocol invariant guard.

WHY
---
AG-UI is a stateful wire protocol: ``TEXT_MESSAGE_CONTENT`` is meaningless
without a preceding ``TEXT_MESSAGE_START``, a ``TOOL_CALL_RESULT`` needs its
``TOOL_CALL_END``, and a run must open with ``RUN_STARTED`` and close exactly
once. Producers violate this constantly — a pre-run hook emits before the run
opens, a hidden tool leaves behind the empty parent text message Agno created to
host it, a model streams pure reasoning and opens a text message with nothing in
it, an exception fires mid-tool-call.

Rather than special-casing each of those at the point where they arise, the
sequencer sits at the very end of the pipeline and enforces the invariants over
whatever it is handed. Upstream stages can then be simple and honest.

USAGE
-----
::

    seq = EventSequencer(thread_id="t1", run_id="r1")
    for event in produced_events:
        for out in seq.feed(event):
            yield out
    for out in seq.finish():
        yield out

MODES
-----
``strict``  raise :class:`ProtocolViolationError` on the first violation
``repair``  fix silently (default — this is what production streams use)
``audit``   fix, and record every violation on ``sequencer.violations``
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ag_ui.core import (
    BaseEvent,
    EventType,
    ReasoningEndEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunStartedEvent,
    StepFinishedEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)

# Events that terminate a run. Exactly one may be emitted.
_TERMINAL = frozenset({EventType.RUN_FINISHED, EventType.RUN_ERROR})

# Events allowed to appear before RUN_STARTED without being buffered.
_PRE_RUN_ALLOWED = frozenset({EventType.RUN_STARTED})


class SequencerMode(StrEnum):
    STRICT = "strict"
    REPAIR = "repair"
    AUDIT = "audit"


@dataclass(frozen=True)
class ProtocolViolation:
    """One invariant breach, with the repair that was applied."""

    rule: str
    detail: str
    event_type: str | None = None
    repair: str = ""
    index: int = -1

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"[{self.rule}] {self.detail} -> {self.repair or 'dropped'}"


class ProtocolViolationError(RuntimeError):
    """Raised in ``strict`` mode when an invariant is breached."""

    def __init__(self, violation: ProtocolViolation) -> None:
        super().__init__(str(violation))
        self.violation = violation


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class _ToolState:
    name: str
    ended: bool = False
    result_sent: bool = False


@dataclass
class EventSequencer:
    """Enforces AG-UI protocol invariants over a stream of events.

    The sequencer is single-use: one instance per run.

    Invariants enforced
    -------------------
    1. ``RUN_STARTED`` is the first frame. Anything produced before it is
       buffered and released immediately after (this is what lets a pre-run hook
       emit a ``CUSTOM`` event without breaking the protocol).
    2. Text messages and reasoning blocks are never empty on the wire. A
       ``START`` is held back until the first ``CONTENT`` with something in it —
       for reasoning, something other than whitespace — and a block that ends
       without one is discarded whole. This is what makes hidden tools, empty
       thinking announcements and pure-reasoning chunks fall out for free.
    3. ``CONTENT``/``END`` without a matching ``START`` auto-opens one, for text,
       tool calls and reasoning alike.
    4. Only one text message, one reasoning session and one result per tool call
       may be open at a time; overlaps auto-close the previous.
    5. Reasoning closes when anything visible starts — an answer or a tool call.
       Agno leaves the session open until the run ends, which would otherwise
       put ``REASONING_END`` *after* the whole answer had streamed and collapse
       think / call / think into one block. Thinking that resumes gets a fresh
       session, so the blocks stay in the order they happened — and a fresh
       message id, because Agno keeps one id for the whole run and a client
       keying blocks by id would merge them back together.
    6. Everything still open is closed before a terminal event.
    7. Exactly one terminal event; frames after it are dropped.
    """

    thread_id: str = ""
    run_id: str = ""
    mode: SequencerMode = SequencerMode.REPAIR
    violations: list[ProtocolViolation] = field(default_factory=list)

    # -- run lifecycle
    _run_started: bool = False
    _terminated: bool = False
    _pre_run_buffer: list[BaseEvent] = field(default_factory=list)
    _index: int = 0

    # -- text message
    _open_text_id: str | None = None
    _deferred_text_start: TextMessageStartEvent | None = None
    _dropped_message_ids: set[str] = field(default_factory=set)

    # -- tool calls
    _tools: dict[str, _ToolState] = field(default_factory=dict)

    # -- reasoning
    _reasoning_id: str | None = None
    _reasoning_message_id: str | None = None
    # The id the producer uses for the open session, the id it goes out under,
    # and every producer id whose session has already been closed.
    _reasoning_source_id: str | None = None
    _reasoning_alias: str | None = None
    _closed_reasoning_ids: set[str] = field(default_factory=set)
    # Frames of a block that has not earned its way onto the wire yet.
    _held_reasoning: list[BaseEvent] = field(default_factory=list)
    _reasoning_shown: bool = False

    # -- steps
    _open_steps: list[str] = field(default_factory=list)

    # ── public API ────────────────────────────────────────────────────────

    def feed(self, event: BaseEvent) -> list[BaseEvent]:
        """Push one event through the guard, returning what should go on the wire."""
        self._index += 1
        return list(self._feed(event))

    def feed_all(self, events: Iterable[BaseEvent]) -> list[BaseEvent]:
        out: list[BaseEvent] = []
        for event in events:
            out.extend(self.feed(event))
        return out

    def finish(self) -> list[BaseEvent]:
        """Close anything still open. Call once, after the last ``feed``.

        Does *not* synthesize a terminal event — the bridge always sends one. If
        buffered pre-run events never got a ``RUN_STARTED``, they are released
        behind a synthesized one so they are not silently lost.
        """
        out: list[BaseEvent] = []
        if not self._run_started and self._pre_run_buffer:
            self._violate(
                "run_started_missing",
                "stream ended without RUN_STARTED but pre-run events were buffered",
                repair="synthesized RUN_STARTED",
            )
            out.extend(self._emit_run_started())
        if not self._terminated:
            out.extend(self._close_all())
        return out

    @property
    def is_terminated(self) -> bool:
        return self._terminated

    # ── dispatch ──────────────────────────────────────────────────────────

    def _feed(self, event: BaseEvent) -> Iterator[BaseEvent]:
        etype = _type_of(event)

        if self._terminated:
            self._violate(
                "after_terminal",
                f"{etype} emitted after the run already ended",
                event_type=etype,
            )
            return

        if etype == EventType.RUN_STARTED:
            if self._run_started:
                self._violate(
                    "duplicate_run_started",
                    "RUN_STARTED emitted twice",
                    event_type=etype,
                )
                return
            yield from self._emit_run_started(event)
            return

        if not self._run_started:
            # Buffer rather than drop: pre-run hooks legitimately produce events
            # before the run opens, they just cannot go on the wire yet.
            if etype not in _PRE_RUN_ALLOWED:
                self._violate(
                    "before_run_started",
                    f"{etype} emitted before RUN_STARTED",
                    event_type=etype,
                    repair="buffered until after RUN_STARTED",
                )
                self._pre_run_buffer.append(event)
            return

        if etype in _TERMINAL:
            yield from self._close_all()
            self._terminated = True
            yield event
            return

        handler = _DISPATCH.get(etype)
        if handler is None:
            yield event  # CUSTOM / STATE_* / RAW / MESSAGES_SNAPSHOT / chunks
            return
        yield from handler(self, event)

    # ── run lifecycle ─────────────────────────────────────────────────────

    def _emit_run_started(self, event: BaseEvent | None = None) -> Iterator[BaseEvent]:
        self._run_started = True
        yield event or RunStartedEvent(
            type=EventType.RUN_STARTED, thread_id=self.thread_id, run_id=self.run_id
        )
        buffered, self._pre_run_buffer = self._pre_run_buffer, []
        for pending in buffered:
            yield from self._feed(pending)

    def _close_all(self) -> Iterator[BaseEvent]:
        """Close reasoning, tools, text and steps — in the order a client expects."""
        yield from self._close_reasoning(reason="run ended")
        for tool_call_id, tool in list(self._tools.items()):
            if not tool.ended:
                self._violate(
                    "unclosed_tool_call",
                    f"tool call {tool_call_id} ({tool.name}) was never ended",
                    repair="auto-emitted TOOL_CALL_END",
                )
                tool.ended = True
                yield ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tool_call_id)
        yield from self._close_text(reason="run ended")
        for step_name in reversed(self._open_steps):
            self._violate(
                "unclosed_step",
                f"step {step_name!r} was never finished",
                repair="auto-emitted STEP_FINISHED",
            )
            yield StepFinishedEvent(type=EventType.STEP_FINISHED, step_name=step_name)
        self._open_steps.clear()

    # ── text messages ─────────────────────────────────────────────────────

    def _on_text_start(self, event: BaseEvent) -> Iterator[BaseEvent]:
        if not isinstance(event, TextMessageStartEvent):
            yield event
            return
        if self._open_text_id is not None or self._deferred_text_start is not None:
            self._violate(
                "overlapping_text_message",
                "TEXT_MESSAGE_START while another message was open",
                event_type=EventType.TEXT_MESSAGE_START,
                repair="closed the previous message first",
            )
            yield from self._close_text(reason="superseded")
        # Held back until the first non-empty delta so empty messages never ship.
        self._deferred_text_start = event

    def _on_text_content(self, event: BaseEvent) -> Iterator[BaseEvent]:
        delta = getattr(event, "delta", "")
        if not delta:
            self._violate(
                "empty_text_content",
                "TEXT_MESSAGE_CONTENT with an empty delta",
                event_type=EventType.TEXT_MESSAGE_CONTENT,
            )
            return

        message_id = getattr(event, "message_id", "") or _new_id("msg")

        if self._deferred_text_start is not None:
            start = self._deferred_text_start
            self._deferred_text_start = None
            # Thinking is over the moment the answer starts. Closing here rather
            # than at TEXT_MESSAGE_START is deliberate: Agno opens an empty text
            # message to host tool calls, and that one must not end the
            # reasoning the model is still in the middle of.
            yield from self._close_reasoning(reason="answer started")
            self._open_text_id = start.message_id
            yield start
        elif self._open_text_id is None:
            self._violate(
                "content_before_start",
                f"TEXT_MESSAGE_CONTENT for {message_id} with no open message",
                event_type=EventType.TEXT_MESSAGE_CONTENT,
                repair="auto-emitted TEXT_MESSAGE_START",
            )
            yield from self._close_reasoning(reason="answer started")
            self._open_text_id = message_id
            yield TextMessageStartEvent(
                type=EventType.TEXT_MESSAGE_START, message_id=message_id, role="assistant"
            )

        if message_id != self._open_text_id:
            # Retarget rather than open a second message: two concurrently open
            # text messages is the one thing a client cannot render.
            self._violate(
                "text_message_id_mismatch",
                f"content for {message_id} while {self._open_text_id} is open",
                event_type=EventType.TEXT_MESSAGE_CONTENT,
                repair=f"retargeted to {self._open_text_id}",
            )
            event = event.model_copy(update={"message_id": self._open_text_id})
        yield event

    def _on_text_end(self, event: BaseEvent) -> Iterator[BaseEvent]:
        if self._deferred_text_start is not None:
            # START immediately followed by END: the message never had content,
            # so neither frame ships. This is the invariant that makes hidden
            # tools and pure-reasoning chunks clean without special-casing them.
            self._violate(
                "empty_text_message",
                f"text message {self._deferred_text_start.message_id} had no content",
                event_type=EventType.TEXT_MESSAGE_END,
                repair="dropped the empty START/END pair",
            )
            self._dropped_message_ids.add(self._deferred_text_start.message_id)
            self._deferred_text_start = None
            return
        if self._open_text_id is None:
            self._violate(
                "end_without_start",
                "TEXT_MESSAGE_END with no open message",
                event_type=EventType.TEXT_MESSAGE_END,
            )
            return
        message_id = getattr(event, "message_id", "")
        if message_id != self._open_text_id:
            event = event.model_copy(update={"message_id": self._open_text_id})
        self._open_text_id = None
        yield event

    def _close_text(self, *, reason: str) -> Iterator[BaseEvent]:
        if self._deferred_text_start is not None:
            self._violate(
                "empty_text_message",
                f"text message {self._deferred_text_start.message_id} had no content ({reason})",
                repair="dropped the empty START",
            )
            self._dropped_message_ids.add(self._deferred_text_start.message_id)
            self._deferred_text_start = None
        if self._open_text_id is not None:
            self._violate(
                "unclosed_text_message",
                f"text message {self._open_text_id} was never ended ({reason})",
                repair="auto-emitted TEXT_MESSAGE_END",
            )
            yield TextMessageEndEvent(
                type=EventType.TEXT_MESSAGE_END, message_id=self._open_text_id
            )
            self._open_text_id = None

    # ── tool calls ────────────────────────────────────────────────────────

    def _on_tool_start(self, event: BaseEvent) -> Iterator[BaseEvent]:
        tool_call_id = getattr(event, "tool_call_id", "")
        name = getattr(event, "tool_call_name", "") or "unknown"
        if tool_call_id in self._tools:
            self._violate(
                "duplicate_tool_call_start",
                f"TOOL_CALL_START repeated for {tool_call_id}",
                event_type=EventType.TOOL_CALL_START,
            )
            return
        # A tool call may not sit inside an open text message or an open
        # reasoning session. Thinking that resumes afterwards is a new session,
        # which is what lets a client render think / call / think / answer as
        # four blocks in the order they happened.
        yield from self._close_reasoning(reason="tool call started")
        yield from self._close_text(reason="tool call started")
        self._tools[tool_call_id] = _ToolState(name=name)
        yield self._clean_parent(event)

    def _on_tool_args(self, event: BaseEvent) -> Iterator[BaseEvent]:
        tool_call_id = getattr(event, "tool_call_id", "")
        yield from self._ensure_tool_open(tool_call_id, EventType.TOOL_CALL_ARGS)
        if not getattr(event, "delta", ""):
            self._violate(
                "empty_tool_args",
                f"TOOL_CALL_ARGS with an empty delta for {tool_call_id}",
                event_type=EventType.TOOL_CALL_ARGS,
            )
            return
        yield event

    def _on_tool_end(self, event: BaseEvent) -> Iterator[BaseEvent]:
        tool_call_id = getattr(event, "tool_call_id", "")
        tool = self._tools.get(tool_call_id)
        if tool is None:
            self._violate(
                "tool_end_without_start",
                f"TOOL_CALL_END for unknown call {tool_call_id}",
                event_type=EventType.TOOL_CALL_END,
            )
            return
        if tool.ended:
            self._violate(
                "duplicate_tool_call_end",
                f"TOOL_CALL_END repeated for {tool_call_id}",
                event_type=EventType.TOOL_CALL_END,
            )
            return
        tool.ended = True
        yield event

    def _on_tool_result(self, event: BaseEvent) -> Iterator[BaseEvent]:
        tool_call_id = getattr(event, "tool_call_id", "")
        yield from self._ensure_tool_open(tool_call_id, EventType.TOOL_CALL_RESULT)
        tool = self._tools[tool_call_id]
        if not tool.ended:
            self._violate(
                "result_before_end",
                f"TOOL_CALL_RESULT for {tool_call_id} before its TOOL_CALL_END",
                event_type=EventType.TOOL_CALL_RESULT,
                repair="auto-emitted TOOL_CALL_END",
            )
            tool.ended = True
            yield ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tool_call_id)
        if tool.result_sent:
            self._violate(
                "duplicate_tool_result",
                f"TOOL_CALL_RESULT repeated for {tool_call_id}",
                event_type=EventType.TOOL_CALL_RESULT,
            )
            return
        tool.result_sent = True
        yield event

    def _clean_parent(self, event: BaseEvent) -> BaseEvent:
        """Drop a ``parent_message_id`` pointing at a message that never shipped.

        Agno opens a text message to host a tool call. When that message stays
        empty the sequencer discards it, which would otherwise leave the tool
        call referencing a message the client has never seen.
        """
        parent = getattr(event, "parent_message_id", None)
        if parent and parent in self._dropped_message_ids:
            return event.model_copy(update={"parent_message_id": None})
        return event

    def _ensure_tool_open(self, tool_call_id: str, source: EventType) -> Iterator[BaseEvent]:
        if tool_call_id in self._tools:
            return
        self._violate(
            "tool_event_without_start",
            f"{source} for {tool_call_id} with no TOOL_CALL_START",
            event_type=source,
            repair="auto-emitted TOOL_CALL_START",
        )
        yield from self._close_reasoning(reason="tool call started")
        yield from self._close_text(reason="tool call started")
        self._tools[tool_call_id] = _ToolState(name="unknown")
        yield ToolCallStartEvent(
            type=EventType.TOOL_CALL_START,
            tool_call_id=tool_call_id,
            tool_call_name="unknown",
        )

    # ── reasoning ─────────────────────────────────────────────────────────

    def _on_reasoning_start(self, event: BaseEvent) -> Iterator[BaseEvent]:
        if self._reasoning_id is not None:
            self._violate(
                "duplicate_reasoning_start",
                "REASONING_START while a reasoning session was already open",
                event_type=EventType.REASONING_START,
            )
            return
        message_id = self._wire_reasoning_id(getattr(event, "message_id", ""))
        self._reasoning_id = message_id
        yield from self._hold(_retarget(event, message_id))

    def _on_reasoning_message_start(self, event: BaseEvent) -> Iterator[BaseEvent]:
        message_id = self._wire_reasoning_id(getattr(event, "message_id", ""))
        event = _retarget(event, message_id)
        held = list(self._ensure_reasoning_open(message_id, EventType.REASONING_MESSAGE_START))
        if self._reasoning_message_id is not None:
            self._violate(
                "overlapping_reasoning_message",
                "REASONING_MESSAGE_START while another reasoning message was open",
                event_type=EventType.REASONING_MESSAGE_START,
                repair="closed the previous reasoning message",
            )
            held.append(
                ReasoningMessageEndEvent(
                    type=EventType.REASONING_MESSAGE_END, message_id=self._reasoning_message_id
                )
            )
        self._reasoning_message_id = message_id
        yield from self._hold(*held, event)

    def _on_reasoning_content(self, event: BaseEvent) -> Iterator[BaseEvent]:
        delta = getattr(event, "delta", "")
        if not delta:
            self._violate(
                "empty_reasoning_content",
                "REASONING_MESSAGE_CONTENT with an empty delta",
                event_type=EventType.REASONING_MESSAGE_CONTENT,
            )
            return
        if not delta.strip() and not self._reasoning_shown:
            # A blank delta is not thinking and cannot open a block, but it is
            # not thrown away either: once real thinking arrives, the paragraph
            # break this encodes goes out in front of it.
            yield from self._hold(event)
            return

        message_id = self._wire_reasoning_id(getattr(event, "message_id", ""))
        held = list(self._ensure_reasoning_open(message_id, EventType.REASONING_MESSAGE_CONTENT))
        if self._reasoning_message_id is None:
            self._violate(
                "reasoning_content_before_start",
                f"REASONING_MESSAGE_CONTENT for {message_id} with no open reasoning message",
                event_type=EventType.REASONING_MESSAGE_CONTENT,
                repair="auto-emitted REASONING_MESSAGE_START",
            )
            self._reasoning_message_id = message_id
            held.append(
                ReasoningMessageStartEvent(
                    type=EventType.REASONING_MESSAGE_START,
                    message_id=message_id,
                    role="reasoning",
                )
            )
        yield from self._show(*held, _retarget(event, self._reasoning_message_id))

    def _on_reasoning_message_end(self, event: BaseEvent) -> Iterator[BaseEvent]:
        if self._reasoning_message_id is None:
            self._violate(
                "reasoning_end_without_start",
                "REASONING_MESSAGE_END with no open reasoning message",
                event_type=EventType.REASONING_MESSAGE_END,
            )
            return
        if not self._reasoning_shown:
            self._drop_held_reasoning()
            return
        event = _retarget(event, self._reasoning_message_id)
        self._reasoning_message_id = None
        yield event

    def _on_reasoning_end(self, event: BaseEvent) -> Iterator[BaseEvent]:
        if self._reasoning_id is None:
            self._violate(
                "reasoning_end_without_start",
                "REASONING_END with no open reasoning session",
                event_type=EventType.REASONING_END,
            )
            return
        if not self._reasoning_shown:
            self._drop_held_reasoning()
            return
        if self._reasoning_message_id is not None:
            yield ReasoningMessageEndEvent(
                type=EventType.REASONING_MESSAGE_END, message_id=self._reasoning_message_id
            )
            self._reasoning_message_id = None
        event = _retarget(event, self._reasoning_id)
        self._reasoning_id = None
        self._reasoning_shown = False
        self._mark_reasoning_closed()
        yield event

    def _ensure_reasoning_open(self, message_id: str, source: EventType) -> Iterator[BaseEvent]:
        if self._reasoning_id is not None:
            return
        self._violate(
            "reasoning_event_without_start",
            f"{source} with no REASONING_START",
            event_type=source,
            repair="auto-emitted REASONING_START",
        )
        self._reasoning_id = message_id
        yield ReasoningStartEvent(type=EventType.REASONING_START, message_id=message_id)

    # A reasoning block is held back until it has something to say, the same way
    # a text message is. Thinking models announce a block and then stream
    # nothing into it, or a lone newline; shipped as-is that is an empty bubble
    # in the transcript, and — because a block forces the open answer closed —
    # an answer torn in half around it.

    def _hold(self, *events: BaseEvent) -> Iterator[BaseEvent]:
        if self._reasoning_shown:
            yield from events
            return
        self._held_reasoning.extend(events)

    def _show(self, *events: BaseEvent) -> Iterator[BaseEvent]:
        """Release what was held, and everything after it, for this session."""
        self._reasoning_shown = True
        held, self._held_reasoning = self._held_reasoning, []
        yield from held
        yield from events

    def _drop_held_reasoning(self) -> None:
        """Forget a block that never said anything, id and all."""
        self._violate(
            "empty_reasoning_block",
            f"reasoning block {self._reasoning_id} had no content",
            repair="dropped the whole block",
        )
        self._held_reasoning = []
        self._reasoning_id = None
        self._reasoning_message_id = None
        self._reasoning_shown = False
        # Not marked closed: nothing went out under this id, so the next block
        # may still use it rather than being remapped away from it.
        self._reasoning_source_id = None
        self._reasoning_alias = None

    def _close_reasoning(self, *, reason: str) -> Iterator[BaseEvent]:
        if self._held_reasoning or (self._reasoning_id is not None and not self._reasoning_shown):
            self._drop_held_reasoning()
            return
        if self._reasoning_message_id is not None:
            self._violate(
                "unclosed_reasoning_message",
                f"reasoning message {self._reasoning_message_id} was never ended ({reason})",
                repair="auto-emitted REASONING_MESSAGE_END",
            )
            yield ReasoningMessageEndEvent(
                type=EventType.REASONING_MESSAGE_END, message_id=self._reasoning_message_id
            )
            self._reasoning_message_id = None
        if self._reasoning_id is not None:
            self._violate(
                "unclosed_reasoning",
                f"reasoning session {self._reasoning_id} was never ended ({reason})",
                repair="auto-emitted REASONING_END",
            )
            yield ReasoningEndEvent(type=EventType.REASONING_END, message_id=self._reasoning_id)
            self._reasoning_id = None
        self._reasoning_shown = False
        self._mark_reasoning_closed()

    def _wire_reasoning_id(self, source_id: str) -> str:
        """The id ``source_id`` goes out under, minting a fresh one on reuse.

        Agno keeps one ``reasoning_message_id`` for a whole run, so the thinking
        that resumes after a tool call arrives under the id of the session this
        sequencer already closed. Left alone, a client that keys blocks by id
        appends the second session into the first.
        """
        if source_id and source_id == self._reasoning_source_id and self._reasoning_alias:
            return self._reasoning_alias
        wire_id = source_id or _new_id("reasoning")
        if wire_id in self._closed_reasoning_ids:
            wire_id = _new_id("reasoning")
            self._violate(
                "reasoning_id_reused",
                f"reasoning id {source_id} reused after its session closed",
                repair=f"remapped to a fresh reasoning message id ({wire_id})",
            )
        self._reasoning_source_id = source_id or wire_id
        self._reasoning_alias = wire_id
        return wire_id

    def _mark_reasoning_closed(self) -> None:
        if self._reasoning_source_id is not None:
            self._closed_reasoning_ids.add(self._reasoning_source_id)
        self._reasoning_source_id = None
        self._reasoning_alias = None

    # ── steps ─────────────────────────────────────────────────────────────

    def _on_step_started(self, event: BaseEvent) -> Iterator[BaseEvent]:
        self._open_steps.append(getattr(event, "step_name", ""))
        yield event

    def _on_step_finished(self, event: BaseEvent) -> Iterator[BaseEvent]:
        step_name = getattr(event, "step_name", "")
        if step_name not in self._open_steps:
            self._violate(
                "step_finished_without_start",
                f"STEP_FINISHED for {step_name!r} which was never started",
                event_type=EventType.STEP_FINISHED,
            )
            return
        self._open_steps.remove(step_name)
        yield event

    # ── violations ────────────────────────────────────────────────────────

    def _violate(
        self,
        rule: str,
        detail: str,
        *,
        event_type: Any = None,
        repair: str = "",
    ) -> None:
        violation = ProtocolViolation(
            rule=rule,
            detail=detail,
            event_type=str(event_type) if event_type is not None else None,
            repair=repair or "dropped",
            index=self._index,
        )
        if self.mode is SequencerMode.STRICT:
            raise ProtocolViolationError(violation)
        if self.mode is SequencerMode.AUDIT:
            self.violations.append(violation)


def _type_of(event: BaseEvent) -> Any:
    return getattr(event, "type", None)


def _retarget(event: BaseEvent, message_id: str | None) -> BaseEvent:
    if message_id is None or getattr(event, "message_id", None) == message_id:
        return event
    return event.model_copy(update={"message_id": message_id})


_DISPATCH = {
    EventType.TEXT_MESSAGE_START: EventSequencer._on_text_start,
    EventType.TEXT_MESSAGE_CONTENT: EventSequencer._on_text_content,
    EventType.TEXT_MESSAGE_END: EventSequencer._on_text_end,
    EventType.TOOL_CALL_START: EventSequencer._on_tool_start,
    EventType.TOOL_CALL_ARGS: EventSequencer._on_tool_args,
    EventType.TOOL_CALL_END: EventSequencer._on_tool_end,
    EventType.TOOL_CALL_RESULT: EventSequencer._on_tool_result,
    EventType.REASONING_START: EventSequencer._on_reasoning_start,
    EventType.REASONING_MESSAGE_START: EventSequencer._on_reasoning_message_start,
    EventType.REASONING_MESSAGE_CONTENT: EventSequencer._on_reasoning_content,
    EventType.REASONING_MESSAGE_END: EventSequencer._on_reasoning_message_end,
    EventType.REASONING_END: EventSequencer._on_reasoning_end,
    EventType.STEP_STARTED: EventSequencer._on_step_started,
    EventType.STEP_FINISHED: EventSequencer._on_step_finished,
}


__all__ = [
    "EventSequencer",
    "ProtocolViolation",
    "ProtocolViolationError",
    "SequencerMode",
]
