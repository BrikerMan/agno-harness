"""Event parsers — supplement Agno's official chunk handlers.

A parser receives the same chunk the official handler just processed and yields
*additional* AG-UI events. It never replaces official output. Register one per
``RunEvent`` category with ``bridge.register_parser``.

Parsers exist because Agno's handlers are lossy in places (they drop
``reasoning_content``) and because some concepts have no Agno equivalent at all
(sub-agent delegation as a progress step).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ag_ui.core import BaseEvent, CustomEvent, EventType, StepFinishedEvent, StepStartedEvent
from agno.os.interfaces.agui.handlers import on_reasoning_content_delta
from agno.os.interfaces.agui.state import StreamState
from agno.run.agent import RunEvent
from agno.run.base import BaseRunOutputEvent

from .types import EventParser


def event_value(chunk: BaseRunOutputEvent) -> str:
    """The chunk's ``RunEvent`` as a plain string.

    Agno stores ``chunk.event`` as the enum's value (``"RunContent"``), but we
    tolerate a real enum too.
    """
    event = getattr(chunk, "event", None)
    if event is None:
        return ""
    return event.value if hasattr(event, "value") else str(event)


def reasoning_content_parser(chunk: BaseRunOutputEvent, state: StreamState) -> Iterable[BaseEvent]:
    """Surface ``reasoning_content`` that Agno's ``on_run_content`` drops.

    Thinking models in the Qwen family attach their chain of thought to the
    ``reasoning_content`` field of the *same* ``RunContent`` chunk that carries
    ordinary content. Agno's official handler reads only ``content``, so live
    streaming shows no reasoning at all — while replay, which reads
    ``run.reasoning_content`` off the persisted run, shows it. That asymmetry is
    the bug this closes.

    The chunk is fed through Agno's own ``on_reasoning_content_delta``, which
    owns the ``REASONING_START`` / ``REASONING_MESSAGE_START`` lifecycle, so we
    inherit its behaviour instead of reimplementing it.

    Blank deltas are the exception, and are dropped unless a thought is already
    running. Acting on one is not free: Agno's handler closes the open text
    message before every reasoning delta, and the model reopens the answer under
    a new message id. A stray ``"\\n"`` of reasoning — which Qwen sprinkles
    between content chunks — therefore cuts the answer in two, in exchange for a
    thinking block with nothing in it. When the cut lands inside a StreamUI
    fence, the header ends up in one message and the body in the next, and the
    card never parses. Newlines arriving mid-thought are ordinary paragraph
    breaks and pass through untouched.

    See :func:`_answer_in_progress` for why neither field on ``StreamState``
    answers "is the model writing an answer right now" on its own.
    """
    if event_value(chunk) != RunEvent.run_content.value:
        return
    if getattr(chunk, "content", None):
        setattr(state, _ANSWERING, state.text_message_id)

    reasoning = getattr(chunk, "reasoning_content", None)
    if not reasoning:
        return
    if not reasoning.strip() and (_answer_in_progress(state) or state.reasoning_message_id is None):
        return
    yield from on_reasoning_content_delta(chunk, state)


#: Which text message the answer is being written into, recorded on the run's
#: own ``StreamState`` so it lives and dies with the run.
_ANSWERING = "_bat_answering_message_id"


def _answer_in_progress(state: StreamState) -> bool:
    """Whether words the user can read are being streamed right now.

    Neither obvious field says this. ``reasoning_message_id`` is set by the
    first thought and never cleared, so it reads "thinking" for the rest of the
    run. ``text_message_open`` is true almost as soon, because Agno opens a text
    message on any content chunk — including the content-free ones that carry
    nothing but reasoning. So the answer is tracked here instead: a message that
    has actually had content written into it, and is still the open one.
    """
    answering = getattr(state, _ANSWERING, None)
    return bool(state.text_message_open and answering and answering == state.text_message_id)


def subagent_steps_parser(delegate_tool_names: Iterable[str]) -> EventParser:
    """Mark sub-agent delegation with ``STEP_STARTED`` / ``STEP_FINISHED``.

    When the agent calls one of the named delegate tools, this emits a step
    bracket named after the ``agent_name`` tool argument, so the UI can show
    "→ code_reviewer" while the sub-agent works.

    The delegate tool's own ``TOOL_CALL_*`` events still flow through — this only
    adds markers. Pair it with a :class:`~agno_relay.HideToolFilter` on
    the same tool if you want the step *instead of* the tool card.

    Register it for both ``tool_call_started`` and ``tool_call_completed``.
    """
    delegate = set(delegate_tool_names or ())

    def parser(chunk: BaseRunOutputEvent, state: StreamState) -> Iterable[BaseEvent]:
        value = event_value(chunk)
        if value not in (
            RunEvent.tool_call_started.value,
            RunEvent.tool_call_completed.value,
        ):
            return
        tool = getattr(chunk, "tool", None)
        tool_name = getattr(tool, "tool_name", None) if tool else None
        if tool_name not in delegate:
            return
        args = getattr(tool, "tool_args", None) or {}
        agent_name = args.get("agent_name") or "sub-agent"
        step_name = f"→ {agent_name}"
        if value == RunEvent.tool_call_started.value:
            yield StepStartedEvent(type=EventType.STEP_STARTED, step_name=step_name)
        else:
            yield StepFinishedEvent(type=EventType.STEP_FINISHED, step_name=step_name)

    return parser


def compression_events_parser(chunk: BaseRunOutputEvent, state: StreamState) -> Iterable[BaseEvent]:
    """Emit ``STEP_STARTED`` / ``STEP_FINISHED`` and ``CUSTOM`` event when context compression occurs.

    Surfaces Agno's background ``CompressionStarted`` and ``CompressionCompleted``
    events on the AG-UI stream as visible progress steps and an in-context
    compression card (like in Cursor), informing the user that intermediate
    context is being compressed and preserved.
    """
    value = event_value(chunk)
    if value in (RunEvent.compression_started.value, "TeamCompressionStarted"):
        yield StepStartedEvent(
            type=EventType.STEP_STARTED,
            step_name="⚡ Compressing context...",
        )
    elif value in (RunEvent.compression_completed.value, "TeamCompressionCompleted"):
        stats = getattr(chunk, "compression_stats", None) or {}
        count = getattr(chunk, "tool_results_compressed", None) or stats.get(
            "tool_results_compressed"
        )
        orig = getattr(chunk, "original_size", None) or stats.get("original_size")
        comp = getattr(chunk, "compressed_size", None) or stats.get("compressed_size")
        checkpoint_text = getattr(chunk, "checkpoint", None) or stats.get("checkpoint")
        saved = stats.get("saved_tokens")
        if saved is None and orig is not None and comp is not None:
            saved = max(0, orig - comp)

        meta: dict[str, Any] = {}
        if count is not None:
            meta["tool_results_compressed"] = count
        if orig is not None:
            meta["original_size"] = orig
        if comp is not None:
            meta["compressed_size"] = comp
        if saved is not None:
            meta["saved_tokens"] = saved

        yield StepFinishedEvent(
            type=EventType.STEP_FINISHED,
            step_name="⚡ Compressing context...",
            metadata=meta or None,  # type: ignore[call-arg]
        )
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="context.compression",
            value={
                "original_tokens": orig,
                "compacted_tokens": comp,
                "saved_tokens": saved,
                "checkpoint": checkpoint_text,
            },
        )


__all__ = [
    "compression_events_parser",
    "event_value",
    "reasoning_content_parser",
    "subagent_steps_parser",
]
