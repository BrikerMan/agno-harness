"""Conformance harnesses.

Two assertions, used by every scenario test.

``assert_valid_agui_sequence`` runs a captured trace back through the sequencer
in ``strict`` mode. Since strict mode raises on the first invariant breach, a
trace that survives it is provably well-formed — and the checker is the very
code that produced the trace, so the two can never drift apart.

``messages_from_events`` is a miniature of the frontend reducer. It used to back
a parity assertion between live streaming and a separate replay implementation;
now that replay hands back the stored frames, it is used to check that reducing
those frames gives what reducing the live stream gave — the round trip, rather
than two implementations being compared.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ag_ui.core import BaseEvent, EventType

from agno_relay.core.sequencer import (
    EventSequencer,
    ProtocolViolationError,
    SequencerMode,
)


def assert_valid_agui_sequence(events: Iterable[BaseEvent]) -> None:
    """Fail if the trace breaks any AG-UI protocol invariant."""
    events = list(events)
    sequencer = EventSequencer(mode=SequencerMode.STRICT)
    index = 0
    try:
        # `index` is read by the handler below to report where the trace broke.
        for index, event in enumerate(events):  # noqa: B007
            sequencer.feed(event)
        sequencer.finish()
    except ProtocolViolationError as exc:
        context = "\n".join(
            f"  {i:>3} {_describe(e)}"
            for i, e in enumerate(events[max(0, index - 5) : index + 2], start=max(0, index - 5))
        )
        raise AssertionError(
            f"Protocol violation at frame {index}: {exc.violation}\nSurrounding frames:\n{context}"
        ) from exc

    _assert_single_terminal(events)


def _assert_single_terminal(events: list[BaseEvent]) -> None:
    terminals = [
        i
        for i, e in enumerate(events)
        if getattr(e, "type", None) in (EventType.RUN_FINISHED, EventType.RUN_ERROR)
    ]
    if not terminals:
        raise AssertionError("Trace has no terminal event (RUN_FINISHED or RUN_ERROR)")
    if len(terminals) > 1:
        raise AssertionError(f"Trace has {len(terminals)} terminal events, expected exactly 1")
    if terminals[0] != len(events) - 1:
        trailing = [_describe(e) for e in events[terminals[0] + 1 :]]
        raise AssertionError(f"Frames after the terminal event: {trailing}")


def messages_from_events(events: Iterable[BaseEvent]) -> list[dict[str, Any]]:
    """Reduce an event trace into messages, the way a client would.

    A miniature version of the frontend reducer, so parity is checked against
    what a client would actually render rather than against the raw frames.
    """
    messages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    blocks: dict[str, dict[str, Any]] = {}

    def ensure_assistant() -> dict[str, Any]:
        nonlocal current
        if current is None:
            current = {
                "role": "assistant",
                "content": "",
                "reasoning": "",
                "toolCalls": [],
                "uiBlocks": [],
            }
            messages.append(current)
        return current

    for event in events:
        etype = getattr(event, "type", None)
        if etype is EventType.TEXT_MESSAGE_CONTENT:
            ensure_assistant()["content"] += getattr(event, "delta", "")
        elif etype is EventType.REASONING_MESSAGE_CONTENT:
            ensure_assistant()["reasoning"] += getattr(event, "delta", "")
        elif etype is EventType.TOOL_CALL_START:
            ensure_assistant()["toolCalls"].append(
                {
                    "id": getattr(event, "tool_call_id", ""),
                    "name": getattr(event, "tool_call_name", ""),
                    "args": "",
                    "result": "",
                }
            )
        elif etype is EventType.TOOL_CALL_ARGS:
            call = _find_call(ensure_assistant(), getattr(event, "tool_call_id", ""))
            if call is not None:
                call["args"] += getattr(event, "delta", "")
        elif etype is EventType.TOOL_CALL_RESULT:
            call = _find_call(ensure_assistant(), getattr(event, "tool_call_id", ""))
            if call is not None:
                call["result"] = getattr(event, "content", "")
        elif etype is EventType.CUSTOM:
            _apply_custom(ensure_assistant(), event, blocks)
    return messages


def _apply_custom(
    message: dict[str, Any], event: BaseEvent, blocks: dict[str, dict[str, Any]]
) -> None:
    name = getattr(event, "name", "")
    value = getattr(event, "value", None) or {}
    block_id = value.get("blockId")
    if not block_id:
        return
    if name == "ui.block.start":
        block = {"blockId": block_id, "index": value.get("index", 0), "items": []}
        blocks[block_id] = block
        message["uiBlocks"].append(block)
    elif name == "ui.item":
        block = blocks.get(block_id)
        if block is not None:
            block["items"].append(
                {
                    k: v
                    for k, v in value.items()
                    if k in ("index", "schema", "data", "resolved", "raw", "error")
                }
            )


def _find_call(message: dict[str, Any], tool_call_id: str) -> dict[str, Any] | None:
    for call in message["toolCalls"]:
        if call["id"] == tool_call_id:
            return call
    return None


def _describe(event: BaseEvent) -> str:
    etype = getattr(getattr(event, "type", None), "value", "?")
    for field in ("message_id", "tool_call_id", "name", "step_name"):
        value = getattr(event, field, None)
        if value:
            return f"{etype}({field}={value})"
    return etype


def to_jsonl(events: Iterable[BaseEvent]) -> str:
    """Serialize a trace as golden-file JSONL."""
    lines = []
    for event in events:
        payload = event.model_dump(mode="json", exclude_none=True)
        payload.pop("timestamp", None)
        payload.pop("raw_event", None)
        lines.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return "\n".join(lines) + "\n"


__all__ = [
    "assert_valid_agui_sequence",
    "messages_from_events",
    "to_jsonl",
]
