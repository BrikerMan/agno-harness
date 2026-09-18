"""Tool filters — drop or rewrite tool-call events on their way to the client.

A filter is any callable ``(BaseEvent) -> BaseEvent | None``: return ``None`` to
drop the event, return an event to keep it (possibly rewritten). Filters chain in
registration order and the chain stops at the first drop.

The classes here are just convenient stateful callables; plain functions and
lambdas register the same way.

The reason filters need state at all: only ``TOOL_CALL_START`` carries the tool
name. ``ARGS``/``END``/``RESULT`` carry nothing but ``tool_call_id``, so a filter
that targets a tool by name has to learn the id-to-name mapping as it goes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ag_ui.core import BaseEvent, EventType


class _ToolNameTracker:
    """Learns ``tool_call_id -> tool_name`` from whichever event carries both."""

    def __init__(self) -> None:
        self._by_call_id: dict[str, str] = {}

    def resolve(self, event: BaseEvent) -> str | None:
        name = _attr(event, "tool_call_name", "toolCallName")
        call_id = _attr(event, "tool_call_id", "toolCallId")
        if name and call_id:
            self._by_call_id[call_id] = name
        if name:
            return name
        if call_id:
            return self._by_call_id.get(call_id)
        return None


class HideToolFilter(_ToolNameTracker):
    """Hide every event belonging to the named tools.

    Covers the whole lifecycle — ``START``, ``ARGS``, ``END``, ``RESULT``.

    Use it for tools the user should not see: a ``load_skill`` that injects an
    internal prompt, a bookkeeping call, a retrieval step you would rather
    present as part of the answer.

    The empty parent text message Agno creates to host a tool call is *not* this
    filter's problem — the sequencer discards empty messages on its own.
    """

    def __init__(self, tool_names: Iterable[str]) -> None:
        super().__init__()
        self.tool_names: set[str] = set(tool_names or ())

    def __call__(self, event: BaseEvent) -> BaseEvent | None:
        name = self.resolve(event)
        if name and name in self.tool_names:
            return None
        return event


class TransformResultFilter(_ToolNameTracker):
    """Rewrite the ``TOOL_CALL_RESULT`` content of one tool.

    Use it to redact secrets, trim a verbose payload down to what the UI needs,
    or swap a raw dump for a friendlier summary.
    """

    def __init__(self, tool_name: str, transform: Callable[[str], str]) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.transform = transform

    def __call__(self, event: BaseEvent) -> BaseEvent | None:
        name = self.resolve(event)
        if getattr(event, "type", None) is not EventType.TOOL_CALL_RESULT:
            return event
        if name != self.tool_name:
            return event
        content = getattr(event, "content", "") or ""
        return event.model_copy(update={"content": self.transform(content)})


class RedactFilter(_ToolNameTracker):
    """Replace occurrences of secret strings in tool args and results.

    Pass the values themselves (API keys, tokens, internal URLs). Empty and
    very short values are ignored so a stray ``""`` cannot blank the stream.
    """

    _MIN_SECRET_LEN = 4

    def __init__(self, secrets: Iterable[str], placeholder: str = "[redacted]") -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= self._MIN_SECRET_LEN]
        self.placeholder = placeholder

    def __call__(self, event: BaseEvent) -> BaseEvent | None:
        if not self.secrets:
            return event
        etype = getattr(event, "type", None)
        field = (
            "delta"
            if etype is EventType.TOOL_CALL_ARGS
            else "content"
            if etype is EventType.TOOL_CALL_RESULT
            else None
        )
        if field is None:
            return event
        value = getattr(event, field, "") or ""
        redacted = value
        for secret in self.secrets:
            redacted = redacted.replace(secret, self.placeholder)
        if redacted == value:
            return event
        return event.model_copy(update={field: redacted})


def _attr(event: Any, *names: str) -> str | None:
    for name in names:
        value = getattr(event, name, None)
        if value:
            return str(value)
    return None


def collect_hidden_tool_names(filters: Iterable[Any]) -> set[str]:
    """Union of the tool names every :class:`HideToolFilter` in a chain hides.

    Replay uses this so a tool hidden while streaming stays hidden in history.
    """
    names: set[str] = set()
    for f in filters:
        if isinstance(f, HideToolFilter):
            names |= set(f.tool_names)
    return names


__all__ = [
    "HideToolFilter",
    "RedactFilter",
    "TransformResultFilter",
    "collect_hidden_tool_names",
]
