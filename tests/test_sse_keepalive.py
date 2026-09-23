"""SSE wire keepalive and thread-list projection."""

from __future__ import annotations

import asyncio

import pytest

from agno_harness.runtime.replay import thread_summary_from_session
from agno_harness.transport.router import SSE_PING_FRAME, _with_sse_ping

from .test_replay import FakeInput, FakeRun, FakeSession


@pytest.mark.asyncio
async def test_sse_ping_emits_at_start_and_while_upstream_is_quiet():
    async def slow():
        await asyncio.sleep(0.25)
        yield 'data: {"type":"RUN_FINISHED"}\n\n'

    chunks: list[str] = []
    async for chunk in _with_sse_ping(slow(), interval_s=0.05):
        chunks.append(chunk)

    # First chunk is always the 0s ping
    assert chunks[0] == SSE_PING_FRAME
    # Periodic pings were also emitted while waiting
    assert chunks.count(SSE_PING_FRAME) >= 3
    assert chunks[-1] == 'data: {"type":"RUN_FINISHED"}\n\n'


@pytest.mark.asyncio
async def test_sse_ping_emits_at_start_even_with_busy_stream():
    async def busy():
        for i in range(3):
            yield f"data: {i}\n\n"

    chunks = [c async for c in _with_sse_ping(busy(), interval_s=1.0)]
    # First chunk is always the initial 0s ping, followed by data frames
    assert chunks[0] == SSE_PING_FRAME
    assert chunks[1:] == ["data: 0\n\n", "data: 1\n\n", "data: 2\n\n"]


def test_list_threads_uses_run_count_not_transcript_rebuild():
    session = FakeSession(
        "t1",
        runs=[
            FakeRun("r1", FakeInput("hello"), "a"),
            FakeRun("r2", FakeInput("again"), "b"),
        ],
        updated_at=2,
        session_data={"session_name": "Demo"},
    )
    summary = thread_summary_from_session(session)
    assert summary == {
        "threadId": "t1",
        "title": "Demo",
        "messageCount": 0,
        "runCount": 2,
        "updatedAt": 2,
    }


def test_thread_summary_from_raw_dict():
    row = thread_summary_from_session(
        {
            "session_id": "s1",
            "session_data": {"session_name": "Named"},
            "runs": [{"run_id": "r1", "input": {"input_content": "hi"}}],
            "updated_at": 9,
        }
    )
    assert row == {
        "threadId": "s1",
        "title": "Named",
        "messageCount": 0,
        "runCount": 1,
        "updatedAt": 9,
    }


def test_thread_summary_skips_empty_sessions():
    assert thread_summary_from_session({"session_id": "empty", "runs": []}) is None
