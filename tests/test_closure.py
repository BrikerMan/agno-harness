"""Tests for clean seal, dangling tool call closure, and smart compression in better-agno-toolbox.

Usage:
    uv run pytest vendor/better-agno-toolbox/tests/test_closure.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ag_ui.core import CustomEvent, EventType, StepFinishedEvent, StepStartedEvent
from agno.db.in_memory import InMemoryDb
from agno.models.message import Message
from agno.run.agent import (
    CompressionCompletedEvent,
    CompressionStartedEvent,
    RunOutput,
)
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from agno_relay.runtime.closure import (
    attach_checkpoint_to_session,
    close_dangling_tool_calls,
    install_sealed_history_hook,
    sanitize_reason,
    seal_session_run,
    strip_stream_ui,
)
from agno_relay.runtime.compression import (
    SmartCompressionManager,
    count_messages_tokens,
    count_string_tokens,
    reorganize_messages_with_checkpoint,
    structural_prune,
)
from agno_relay.runtime.parsers import compression_events_parser


def test_sanitize_reason():
    short = "Normal cancellation"
    assert sanitize_reason(short) == short

    long_tb = (
        "Traceback (most recent call last):\n"
        "  File 'foo.py', line 10, in bar\n"
        "    baz()\n"
        "RuntimeError: Underlying connection failed"
    )
    sanitized = sanitize_reason(long_tb)
    assert sanitized == "RuntimeError: Underlying connection failed"

    oversized = "a" * 400
    assert len(sanitize_reason(oversized, max_len=100)) <= 103
    assert sanitize_reason(oversized, max_len=100).endswith("...")


def test_close_dangling_tool_calls_single():
    messages = [
        Message(role="user", content="Search for something"),
        Message(
            role="assistant",
            content="Searching...",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "search_db", "arguments": '{"query": "games"}'},
                }
            ],
        ),
    ]

    closed = close_dangling_tool_calls(messages, reason="User cancelled", is_error=False)
    assert len(closed) == 3
    tool_msg = closed[-1]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "call_123"
    assert tool_msg.tool_name == "search_db"

    data = json.loads(tool_msg.content)
    assert data["status"] == "interrupted"
    assert data["note"] == "User cancelled"


def test_close_dangling_tool_calls_parallel():
    messages = [
        Message(role="user", content="Fetch data in parallel"),
        Message(
            role="assistant",
            content="Running tools...",
            tool_calls=[
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "fetch_a", "arguments": "{}"},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "fetch_b", "arguments": "{}"},
                },
                {
                    "id": "call_c",
                    "type": "function",
                    "function": {"name": "fetch_c", "arguments": "{}"},
                },
            ],
        ),
        # call_b completed before cancellation
        Message(role="tool", tool_call_id="call_b", tool_name="fetch_b", content='{"ok": true}'),
    ]

    closed = close_dangling_tool_calls(messages, reason="Task timed out", is_error=True)
    assert len(closed) == 5

    # Check newly appended synthetic messages
    synthesized_ids = {m.tool_call_id for m in closed[3:]}
    assert synthesized_ids == {"call_a", "call_c"}
    for m in closed[3:]:
        assert m.role == "tool"
        assert m.tool_call_error is True
        data = json.loads(m.content)
        assert data["status"] == "error"
        assert data["note"] == "Task timed out"


def test_close_dangling_ask_user():
    messages = [
        Message(role="user", content="Hi"),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "call_ask",
                    "type": "function",
                    "function": {"name": "ask_user", "arguments": '{"question": "Confirm?"}'},
                }
            ],
        ),
    ]

    closed = close_dangling_tool_calls(messages, reason="User skipped")
    assert len(closed) == 3
    data = json.loads(closed[-1].content)
    assert data["status"] == "skipped"


def test_no_dangling_leaves_untouched():
    messages = [
        Message(role="user", content="Hi"),
        Message(
            role="assistant",
            tool_calls=[
                {"id": "call_1", "type": "function", "function": {"name": "f1", "arguments": "{}"}}
            ],
        ),
        Message(role="tool", tool_call_id="call_1", tool_name="f1", content="done"),
    ]
    closed = close_dangling_tool_calls(messages)
    assert len(closed) == 3


def test_seal_session_run_and_history_hook():
    asyncio.run(_async_seal_session_run_and_history_hook())


async def _async_seal_session_run_and_history_hook():
    install_sealed_history_hook()

    db = InMemoryDb()
    session_id = "test-session-1"

    run_output = RunOutput(
        run_id="run-1",
        agent_id="test-agent",
        session_id=session_id,
        status=RunStatus.cancelled,
        messages=[
            Message(role="user", content="Calculate numbers"),
            Message(
                role="assistant",
                content="Step 1...",
                tool_calls=[
                    {
                        "id": "call_calc",
                        "type": "function",
                        "function": {"name": "calc", "arguments": "{}"},
                    }
                ],
            ),
        ],
    )

    session = AgentSession(session_id=session_id, runs=[run_output])
    db.upsert_session(session)

    # Before sealing: Agno get_messages() skips cancelled run
    history_before = session.get_messages()
    assert len(history_before) == 0

    # Seal the run
    ok = await seal_session_run(db, session_id, "run-1", reason="Stopped by user")
    assert ok is True

    # Read back from DB
    loaded = db.get_session(session_id=session_id)
    assert loaded is not None
    sealed_run = loaded.runs[0]

    # Verify status is NOT tampered with (still cancelled!)
    assert sealed_run.status == RunStatus.cancelled
    assert sealed_run.metadata["sealed"] is True
    assert sealed_run.metadata["interrupted"] is True
    assert sealed_run.metadata["interruption_reason"] == "Stopped by user"

    # Verify dangling tool call is closed
    assert len(sealed_run.messages) == 3
    assert sealed_run.messages[-1].role == "tool"
    assert sealed_run.messages[-1].tool_call_id == "call_calc"

    # Verify history hook includes the sealed run
    history_after = loaded.get_messages()
    assert len(history_after) == 3
    # Check that in loaded object, status was restored to cancelled after get_messages()
    assert sealed_run.status == RunStatus.cancelled


def test_structural_prune():
    small = '{"a": 1, "b": 2}'
    assert structural_prune(small, max_chars=100) == small

    large_list = json.dumps([{"id": i, "data": "value" * 10} for i in range(50)])
    pruned = structural_prune(large_list, max_chars=300)
    assert len(pruned) <= 300
    assert "preview_first_3" in pruned
    assert "total_items" in pruned


def test_tiktoken_token_counting():
    text = "Hello world, this is a test prompt with some reasonable length."
    tokens = count_string_tokens(text)
    assert tokens > 5

    messages = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="Calculate the sum of 10 and 20."),
        Message(
            role="assistant",
            content="Thinking...",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "calculator", "arguments": '{"a": 10, "b": 20}'},
                }
            ],
        ),
        Message(role="tool", tool_call_id="call_1", content='{"result": 30}'),
    ]
    total_tokens = count_messages_tokens(messages)
    assert total_tokens > 20


def test_reorganize_messages_with_checkpoint():
    messages = [
        Message(role="system", content="System instruction"),
        Message(role="user", content="Step 1: search info"),
        Message(role="assistant", content="Found info 1"),
        Message(role="user", content="Step 2: search more"),
        Message(role="assistant", content="Found info 2"),
        Message(role="user", content="Step 3: final calculation"),
        Message(
            role="assistant",
            content="Let me calculate",
            tool_calls=[{"id": "call_calc", "function": {"name": "calc"}}],
        ),
        Message(role="tool", tool_call_id="call_calc", content="42"),
    ]

    checkpoint_text = "# CONTEXT CHECKPOINT\n- Step 1 & 2 done.\n- Ready for calculation."
    reorganized = reorganize_messages_with_checkpoint(messages, checkpoint_text)

    # Reorganized structure:
    # 1. System instructions
    # 2. Checkpoint user message
    # 3. Checkpoint assistant ack
    # 4. Active user message (Step 3: final calculation)
    # 5. Active assistant tool call message
    # 6. Active tool result message
    assert len(reorganized) == 6
    assert reorganized[0].role == "system"
    assert reorganized[0].content == "System instruction"
    assert reorganized[1].role == "user"
    assert checkpoint_text in reorganized[1].content
    assert reorganized[2].role == "assistant"
    assert reorganized[3].role == "user"
    assert reorganized[3].content == "Step 3: final calculation"
    assert reorganized[4].role == "assistant"
    assert reorganized[4].tool_calls is not None
    assert reorganized[5].role == "tool"
    assert reorganized[5].content == "42"

    # Pre-turn test: current turn only has the active user prompt;
    # previous turns' tool calls must NOT trail after the user prompt.
    pre_turn_messages = [
        Message(role="system", content="System instruction"),
        Message(role="user", content="Turn 1: Get weather"),
        Message(role="assistant", tool_calls=[{"id": "call_1", "function": {"name": "weather"}}]),
        Message(role="tool", tool_call_id="call_1", content="sunny"),
        Message(role="assistant", content="It is sunny."),
        Message(role="user", content="Turn 2: What should I wear?"),
    ]
    reorganized_pre = reorganize_messages_with_checkpoint(pre_turn_messages, checkpoint_text)
    assert len(reorganized_pre) == 4
    assert reorganized_pre[0].role == "system"
    assert reorganized_pre[1].role == "user"
    assert checkpoint_text in reorganized_pre[1].content
    assert reorganized_pre[2].role == "assistant"
    assert reorganized_pre[3].role == "user"
    assert reorganized_pre[3].content == "Turn 2: What should I wear?"


def test_strip_stream_ui():
    mixed = (
        "Here is the result:\n"
        "<stream-ui>\n"
        '<component name="TaskPlanCard" props="{}"/>\n'
        "</stream-ui>\n"
        "Let me know if you need anything else!"
    )
    cleaned = strip_stream_ui(mixed)
    assert "<stream-ui>" not in cleaned
    assert "TaskPlanCard" not in cleaned
    assert "Here is the result:" in cleaned
    assert "Let me know if you need anything else!" in cleaned

    unclosed = 'Starting card: <stream-ui><component name="Test"'
    assert strip_stream_ui(unclosed) == "Starting card:"


def test_smart_compression_manager():
    asyncio.run(_async_smart_compression_manager())


async def _async_smart_compression_manager():
    # Model mock that responds with a checkpoint
    class MockModel:
        def __init__(self):
            self.id = "mock-model"

        async def aresponse(self, messages: list[Any], **kwargs: Any) -> Any:
            class MockResponse:
                content = (
                    "# CONTEXT CHECKPOINT\n"
                    "- User asked to process multiple batches.\n"
                    "- Completed batches 1 through 5.\n"
                    "- Current batch result: 42."
                )

            return MockResponse()

    manager = SmartCompressionManager(
        model=MockModel(),  # type: ignore[arg-type]
        trigger_token_limit=50,  # Low threshold to trigger compression
        min_messages=3,
        debug_mode=True,
    )

    messages = [
        Message(role="system", content="System instruction"),
        Message(role="user", content="Step 1: run first operation"),
        Message(role="assistant", content="Running operation..."),
        Message(role="tool", content="Intermediate log: " + ("x" * 200)),
        Message(role="user", content="Step 2: run final calculation"),
        Message(
            role="assistant",
            content="Invoking calc tool",
            tool_calls=[{"id": "call_1", "function": {"name": "calc"}}],
        ),
        Message(role="tool", tool_call_id="call_1", content="42"),
    ]

    # Gate check: tokens exceed 50, so compression should trigger
    assert await manager.ashould_compress(messages) is True

    # Run in-context compression
    await manager.acompress(messages)

    # Checkpoint was generated and messages were reorganized in-place
    assert manager.stats["checkpoints_generated"] == 1
    assert manager.last_checkpoint is not None
    assert "# CONTEXT CHECKPOINT" in manager.last_checkpoint["content"]

    # In-place messages now have checkpoint
    assert messages[0].content == "System instruction"
    assert messages[1].role == "user"
    assert "# CONTEXT CHECKPOINT" in messages[1].content
    assert messages[2].role == "assistant"
    assert messages[3].role == "user"
    assert messages[3].content == "Step 2: run final calculation"
    assert messages[4].role == "assistant"
    assert messages[5].role == "tool"
    assert messages[5].content == "42"


def test_checkpoint_multi_turn_history():
    asyncio.run(_async_checkpoint_multi_turn_history())


async def _async_checkpoint_multi_turn_history():
    db = InMemoryDb()
    session_id = "test-session-checkpoint"

    # Turn 1: 5 tool calls, then finished with a checkpoint attached
    run_1 = RunOutput(
        run_id="run-1",
        agent_id="test-agent",
        session_id=session_id,
        status=RunStatus.completed,
        messages=[
            Message(role="user", content="Turn 1: Research AI agents"),
            Message(
                role="assistant",
                content="Searching...",
                tool_calls=[{"id": "c1", "function": {"name": "search"}}],
            ),
            Message(role="tool", tool_call_id="c1", content="Lots of search results"),
            Message(
                role="assistant",
                content="Here is the summary:\n<stream-ui><component name='Card'/></stream-ui>\nAll done!",
            ),
        ],
    )

    # Turn 2: Follow-up turn
    run_2 = RunOutput(
        run_id="run-2",
        agent_id="test-agent",
        session_id=session_id,
        status=RunStatus.completed,
        messages=[
            Message(role="user", content="Turn 2: What about costs?"),
            Message(role="assistant", content="Costs are minimal."),
        ],
    )

    session = AgentSession(session_id=session_id, runs=[run_1, run_2])
    db.upsert_session(session)

    # Attach checkpoint to run-1
    checkpoint_data = {
        "content": "# CONTEXT CHECKPOINT\n- Researched AI agents successfully.",
        "tokens": 15,
    }
    await attach_checkpoint_to_session(db, session_id, "run-1", checkpoint_data)

    loaded = db.get_session(session_id=session_id)
    assert loaded is not None

    # Load messages through checkpoint-aware hook
    history = loaded.get_messages()

    # Expected history:
    # 1. User checkpoint message
    # 2. Assistant checkpoint ack
    # 3. Turn 1 user query
    # 4. Turn 1 final assistant response (without intermediate search tools, without <stream-ui>)
    # 5. Turn 2 user query
    # 6. Turn 2 assistant response
    assert len(history) == 6
    assert history[0].role == "user"
    assert history[0].name == "context_checkpoint"
    assert checkpoint_data["content"] in history[0].content
    assert history[1].role == "assistant"
    assert history[1].name == "context_checkpoint_ack"
    assert history[2].role == "user"
    assert history[2].content == "Turn 1: Research AI agents"
    assert history[3].role == "assistant"
    assert "<stream-ui>" not in history[3].content
    assert history[3].content == "Here is the summary:\n\nAll done!"
    assert history[4].role == "user"
    assert history[4].content == "Turn 2: What about costs?"
    assert history[5].role == "assistant"
    assert history[5].content == "Costs are minimal."


def test_compression_events_parser():
    start_event = CompressionStartedEvent()
    started_parsed = list(compression_events_parser(start_event, None))  # type: ignore
    assert len(started_parsed) == 1
    assert isinstance(started_parsed[0], StepStartedEvent)
    assert started_parsed[0].type == EventType.STEP_STARTED
    assert "Compressing" in str(started_parsed[0].step_name)

    complete_event = CompressionCompletedEvent(
        tool_results_compressed=3,
        original_size=5000,
        compressed_size=800,
    )
    complete_parsed = list(compression_events_parser(complete_event, None))  # type: ignore
    assert len(complete_parsed) == 2
    assert isinstance(complete_parsed[0], StepFinishedEvent)
    assert complete_parsed[0].type == EventType.STEP_FINISHED
    assert complete_parsed[0].metadata["tool_results_compressed"] == 3
    assert isinstance(complete_parsed[1], CustomEvent)
    assert complete_parsed[1].name == "context.compression"
    assert complete_parsed[1].value["original_tokens"] == 5000
    assert complete_parsed[1].value["compacted_tokens"] == 800
    assert complete_parsed[1].value["saved_tokens"] == 4200


def test_checkpoint_subsequent_runs_not_truncated_by_last_n_runs():
    """Verify that after a checkpoint, subsequent incremental runs are all preserved,

    even if get_messages is invoked with a restrictive last_n_runs argument.
    """
    install_sealed_history_hook()
    db = InMemoryDb()
    session_id = "test-checkpoint-subsequent"

    runs = [
        RunOutput(
            run_id=f"run-{i}",
            agent_id="test-agent",
            session_id=session_id,
            status=RunStatus.completed,
            messages=[
                Message(role="user", content=f"Q{i}"),
                Message(role="assistant", content=f"A{i}"),
            ],
        )
        for i in range(5)
    ]
    # Mark run-1 as checkpoint
    runs[1].metadata = {"checkpoint": {"content": "# CHECKPOINT AT RUN 1", "tokens": 20}}

    session = AgentSession(session_id=session_id, runs=runs)
    db.upsert_session(session)

    # Calling get_messages with last_n_runs=1 should NOT discard run-2, run-3, run-4
    loaded = db.get_session(session_id=session_id)
    assert loaded is not None
    messages = loaded.get_messages(last_n_runs=1)

    # Expected:
    # 1. User checkpoint from run-1
    # 2. Assistant checkpoint ack
    # 3. run-1 user
    # 4. run-1 assistant
    # 5. run-2 user & assistant
    # 6. run-3 user & assistant
    # 7. run-4 user & assistant
    # Total: 2 (cp user+ack) + 2 (run-1) + 6 (subsequent runs 2, 3, 4) = 10 messages
    assert len(messages) == 10
    assert messages[0].name == "context_checkpoint"
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"
    user_queries = [
        m.content for m in messages if m.role == "user" and m.name != "context_checkpoint"
    ]
    assert user_queries == ["Q1", "Q2", "Q3", "Q4"]


def test_unbounded_history_reads_all_runs_when_none():
    """Verify that when last_n_runs=None, all historical runs are loaded into context."""
    install_sealed_history_hook()
    db = InMemoryDb()
    session_id = "test-unbounded-history"

    runs = [
        RunOutput(
            run_id=f"run-{i}",
            agent_id="test-agent",
            session_id=session_id,
            status=RunStatus.completed,
            messages=[
                Message(role="user", content=f"Question {i}"),
                Message(role="assistant", content=f"Answer {i}"),
            ],
        )
        for i in range(8)
    ]

    session = AgentSession(session_id=session_id, runs=runs)
    db.upsert_session(session)

    loaded = db.get_session(session_id=session_id)
    assert loaded is not None
    messages = loaded.get_messages(last_n_runs=None)

    assert len(messages) == 16
    user_msgs = [m.content for m in messages if m.role == "user"]
    assert len(user_msgs) == 8
    assert user_msgs[0] == "Question 0"
    assert user_msgs[-1] == "Question 7"


async def test_interrupted_artifact_preserved_in_history_messages():
    """Verify that interrupted streaming document/artifact output is preserved in session history."""
    install_sealed_history_hook()
    db = InMemoryDb()
    session_id = "test-interrupted-artifact"

    # Simulated unclosed stream-ui artifact (like the Distributed Event Architecture Audit in user's prompt)
    partial_audit_doc = (
        "Here is the architecture report:\n"
        '<stream-ui component="artifact" title="Distributed Event Architecture Audit" path="reports/event-mesh-audit.md">\n'
        "# Distributed Event Architecture Audit\n"
        "## 1.3 Event Flow Architecture\n"
        "At-Least-Once Delivery: Enabled via enable.auto.commit=false\n"
        "## 2. Performance Evaluation\n"
        "Produce Throughput: Single Partition 12,400 msg/s, 12 Partitions 131,500 msg/s\n"
    )

    # In Agno, when aborted/cancelled mid-stream, run.messages might only have [system, user]
    run1 = RunOutput(
        run_id="run-audit-interrupted",
        agent_id="test-agent",
        session_id=session_id,
        status=RunStatus.cancelled,
        messages=[
            Message(role="system", content="You are an architect."),
            Message(role="user", content="Write a distributed event architecture report"),
        ],
    )
    session = AgentSession(session_id=session_id, runs=[run1])
    db.upsert_session(session)

    # Seal the run with the streamed partial content
    await seal_session_run(
        db=db,
        session_id=session_id,
        run_id="run-audit-interrupted",
        reason="cancelled",
        partial_content=partial_audit_doc,
    )

    loaded = db.get_session(session_id=session_id)
    assert loaded is not None
    messages = loaded.get_messages()

    # Must contain the user query and the assistant's partial document
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    assert len(assistant_msgs) == 1
    content = assistant_msgs[0].content
    assert "Distributed Event Architecture Audit" in content
    assert "At-Least-Once Delivery" in content
    assert "131,500 msg/s" in content
    assert "(interrupted)" in content
