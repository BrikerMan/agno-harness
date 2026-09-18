"""User-query envelope: raw ask in <user-query>, time/user in <context>."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from agno_harness import AgentRuntime, QueryTurn, SourceContextPlugin, UserQueryBuilder
from agno_harness.core.prompt import is_user_query_envelope, parse_sent_at
from tests.conftest import FakeAgent, content, make_input, run_completed

SHANGHAI = ZoneInfo("Asia/Shanghai")
SENT = datetime(2026, 9, 18, 5, 32, 0, tzinfo=UTC)


async def test_envelope_puts_time_and_user_in_context():
    builder = UserQueryBuilder(plugins=[], timezone=SHANGHAI)
    env = await builder.build(QueryTurn(text="今天下午开会", sent_at=SENT, sender="eliyar"))
    assert env.user_query_text == "今天下午开会"
    assert env.user_query_block == "<user-query>\n今天下午开会\n</user-query>"
    assert "[2026-09-18 13:32:00 +0800]" not in env.user_query_block
    assert "<time>2026-09-18 13:32:00 +0800</time>" in env.context_text
    assert "<user>eliyar</user>" in env.context_text
    assert env.agent_input.startswith("<user-query>")
    assert "```" not in env.agent_input


async def test_envelope_puts_source_in_context():
    builder = UserQueryBuilder(plugins=[SourceContextPlugin()], timezone=SHANGHAI)
    env = await builder.build(
        QueryTurn(
            text="hello",
            sent_at=SENT,
            sender="bob",
            platform="lark",
            is_direct_message=True,
        )
    )
    assert env.user_query_block == "<user-query>\nhello\n</user-query>"
    assert "<time>2026-09-18 13:32:00 +0800</time>" in env.context_text
    assert "<user>bob</user>" in env.context_text
    assert "<platform>lark</platform>" in env.context_text
    assert "<source-type>dm</source-type>" in env.context_text


async def test_envelope_is_idempotent():
    builder = UserQueryBuilder(plugins=[], timezone=SHANGHAI)
    first = await builder.build(QueryTurn(text="hi", sent_at=SENT))
    again = await builder.build(QueryTurn(text=first.agent_input, sent_at=SENT))
    assert again.agent_input == first.agent_input
    assert is_user_query_envelope(first.agent_input)


def test_parse_sent_at_millis_and_iso():
    dt = parse_sent_at(1_758_168_720_000)
    assert dt is not None
    assert dt.year == 2025
    assert parse_sent_at("2026-09-18T13:32:00+08:00") is not None
    assert parse_sent_at(None) is None


@pytest.mark.asyncio
async def test_runtime_wraps_arun_input():
    agent = FakeAgent([content("ok"), run_completed()])
    runtime = AgentRuntime(
        agent=agent,
        user_query_builder=UserQueryBuilder(plugins=[], timezone=SHANGHAI),
    )
    events = [
        e
        async for e in runtime.stream_events(
            make_input(text="今天下午开会", thread_id="t-q", run_id="r-q"),
            metadata={"sent_at": SENT.isoformat(), "sender_name": "eliyar"},
        )
    ]
    assert events
    prompt = agent.last_kwargs["input"]
    assert "<user-query>\n今天下午开会\n</user-query>" in prompt
    assert "<time>2026-09-18 13:32:00 +0800</time>" in prompt
    assert "<user>eliyar</user>" in prompt


@pytest.mark.asyncio
async def test_runtime_can_disable_wrap():
    agent = FakeAgent([content("ok"), run_completed()])
    runtime = AgentRuntime(agent=agent, enable_user_query=False)
    async for _ in runtime.stream_events(make_input(text="plain")):
        pass
    assert agent.last_kwargs["input"] == "plain"
