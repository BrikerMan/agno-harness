"""Shared fixtures and a scripted fake agent.

Every runtime test runs against :class:`FakeAgent` rather than a model. Scripted
chunks make the awkward cases — a tool that never completes, content before its
message opens, a double completion — reproducible, which they are not with a
real LLM.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

import pytest
from ag_ui.core import RunAgentInput
from ag_ui.core.types import UserMessage
from agno.models.response import ToolExecution
from agno.run.agent import (
    ReasoningCompletedEvent,
    ReasoningContentDeltaEvent,
    ReasoningStartedEvent,
    RunCompletedEvent,
    RunContentEvent,
    RunPausedEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)


class FakeAgent:
    """An agent that replays a fixed list of chunks.

    ``arun`` accepts and ignores every keyword the runtime passes, so the runtime
    exercises its real call signature. The keywords it received are recorded on
    ``last_kwargs`` for tests that assert on wiring.
    """

    def __init__(self, chunks: Iterable[Any] | None = None, *, raise_at: int | None = None):
        self.chunks = list(chunks or [])
        # Raise instead of yielding chunk number ``raise_at``. An index past the
        # end means the stream dies after its last chunk, which is the common
        # real-world shape: some output, then the provider drops the connection.
        self.raise_at = raise_at
        self.last_kwargs: dict[str, Any] = {}
        self.db = None

    def arun(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.last_kwargs = kwargs
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        for index, chunk in enumerate(self.chunks):
            if index == self.raise_at:
                raise RuntimeError("model exploded")
            yield chunk
        if self.raise_at is not None and self.raise_at >= len(self.chunks):
            raise RuntimeError("model exploded")


# ── chunk builders ────────────────────────────────────────────────────────


def content(text: str, *, reasoning: str | None = None) -> RunContentEvent:
    return RunContentEvent(content=text, reasoning_content=reasoning)


def reasoning_started() -> ReasoningStartedEvent:
    return ReasoningStartedEvent()


def reasoning_delta(text: str) -> ReasoningContentDeltaEvent:
    return ReasoningContentDeltaEvent(reasoning_content=text)


def reasoning_completed() -> ReasoningCompletedEvent:
    return ReasoningCompletedEvent()


def tool_execution(
    tool_call_id: str,
    tool_name: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
    **extra: Any,
) -> ToolExecution:
    return ToolExecution(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_args=args or {},
        result=result,
        **extra,
    )


def tool_started(
    tool_call_id: str, tool_name: str, args: dict[str, Any] | None = None
) -> ToolCallStartedEvent:
    return ToolCallStartedEvent(tool=tool_execution(tool_call_id, tool_name, args))


def tool_completed(
    tool_call_id: str, tool_name: str, result: Any, args: dict[str, Any] | None = None
) -> ToolCallCompletedEvent:
    return ToolCallCompletedEvent(tool=tool_execution(tool_call_id, tool_name, args, result=result))


def run_completed(text: str = "", **extra: Any) -> RunCompletedEvent:
    return RunCompletedEvent(content=text or None, **extra)


def run_paused(tools: list[ToolExecution]) -> RunPausedEvent:
    """A run waiting on the user. ``tools`` carry the ``requires_*`` flags."""
    return RunPausedEvent(tools=tools)


# ── fixtures ──────────────────────────────────────────────────────────────


def make_input(
    text: str = "hello",
    *,
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    state: Any = None,
    tools: list[Any] | None = None,
    messages: list[Any] | None = None,
) -> RunAgentInput:
    return RunAgentInput(
        thread_id=thread_id,
        run_id=run_id,
        state=state,
        messages=messages
        if messages is not None
        else [UserMessage(id="m1", role="user", content=text)],
        tools=tools or [],
        context=[],
        forwarded_props=None,
    )


@pytest.fixture
def run_input() -> RunAgentInput:
    return make_input()


async def collect(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


def types_of(events: Iterable[Any]) -> list[str]:
    return [getattr(getattr(e, "type", None), "value", str(e)) for e in events]


def text_of(events: Iterable[Any]) -> str:
    """Concatenate every text delta, i.e. what the user actually reads."""
    from ag_ui.core import EventType

    return "".join(
        getattr(e, "delta", "")
        for e in events
        if getattr(e, "type", None) is EventType.TEXT_MESSAGE_CONTENT
    )


def customs(events: Iterable[Any], name: str | None = None) -> list[Any]:
    from ag_ui.core import EventType

    return [
        e
        for e in events
        if getattr(e, "type", None) is EventType.CUSTOM
        and (name is None or getattr(e, "name", None) == name)
    ]
