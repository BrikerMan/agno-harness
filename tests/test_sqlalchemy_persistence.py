from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agno_harness import (
    AgentRuntime,
    ConversationKey,
    DefaultRelayBase,
    OutboundMessage,
    RelayApp,
    SessionRecord,
    SQLAlchemyActionStore,
    SQLAlchemySessionStore,
    SQLAlchemySink,
    Stores,
)
from agno_harness.core.channel import ChannelEvent


@pytest.fixture
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(DefaultRelayBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(async_engine):
    return async_sessionmaker(async_engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_sqlalchemy_session_store_custom_table(session_factory, async_engine):
    store = SQLAlchemySessionStore(session_factory, table_name="custom_user_sessions")
    async with async_engine.begin() as conn:
        await conn.run_sync(DefaultRelayBase.metadata.create_all)

    key = "teams:chat-1:user-42"
    now = datetime.now(UTC)
    record = SessionRecord(
        session_key=key,
        agno_session_id="agno-session-999",
        started_at=now,
        last_active_at=now,
        metadata={"tenant": "acme-corp", "roles": ["admin"]},
    )

    await store.save(record)
    loaded = await store.get(key)

    assert loaded is not None
    assert loaded.session_key == key
    assert loaded.agno_session_id == "agno-session-999"
    assert loaded.metadata["tenant"] == "acme-corp"


@pytest.mark.asyncio
async def test_sqlalchemy_action_store(session_factory, async_engine):
    action_store = SQLAlchemyActionStore(session_factory, table_name="custom_interactive_actions")
    async with async_engine.begin() as conn:
        await conn.run_sync(DefaultRelayBase.metadata.create_all)

    ref = await action_store.create_action(
        action_id="agno.hitl.resume",
        session_key="teams:chat-1",
        agno_session_id="session-123",
        payload={"tool_call_id": "call_456", "path": "/etc/hosts"},
        run_id="run-789",
        tool_call_id="call_456",
        meta={"platform": "teams", "channel_id": "chan-1"},
    )
    assert ref is not None

    record = await action_store.get_action(tool_call_id="call_456")
    assert record is not None
    assert record["action_id"] == "agno.hitl.resume"
    assert record["status"] == "pending"
    assert record["meta"]["platform"] == "teams"

    await action_store.update_status(
        action_id="agno.hitl.resume",
        status="approved",
        tool_call_id="call_456",
        result={"decision": "allow"},
    )

    updated = await action_store.get_action(tool_call_id="call_456")
    assert updated is not None
    assert updated["status"] == "approved"
    assert updated["result"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_sqlalchemy_sink(session_factory, async_engine):
    sink = SQLAlchemySink(session_factory, table_name="custom_audit_log")
    async with async_engine.begin() as conn:
        await conn.run_sync(DefaultRelayBase.metadata.create_all)

    key = ConversationKey(platform="teams", chat_id="chat-audit", sender_id="user-1")
    event = ChannelEvent(
        event_id="e-1",
        key=key,
        text="Please delete production cluster",
        action_id="btn_delete",
    )
    await sink.record_inbound(event, "session-audit-1")

    outbound = OutboundMessage(
        text="Confirmation card sent.",
        cards=[{"type": "AdaptiveCard"}],
        extra={"urgent": True},
    )
    await sink.record_outbound(key, outbound, "session-audit-1")

    history = await sink.get_history("chat-audit")
    assert len(history) == 2
    assert history[0]["direction"] == "inbound"
    assert history[0]["text"] == "Please delete production cluster"
    assert history[1]["direction"] == "outbound"
    assert history[1]["text"] == "Confirmation card sent."
    assert len(history[1]["cards"]) == 1


@pytest.mark.asyncio
async def test_relay_app_action_persistence_and_custom_frame(
    session_factory, async_engine, monkeypatch
):
    from tests.conftest import content, run_completed

    action_store = SQLAlchemyActionStore(session_factory)
    session_store = SQLAlchemySessionStore(session_factory)

    async with async_engine.begin() as conn:
        await conn.run_sync(DefaultRelayBase.metadata.create_all)

    # Mock CustomEventStore
    mock_custom_events = MagicMock()
    saved_custom_frames = []

    async def fake_save(thread_id, run_id, name, value):
        saved_custom_frames.append({"name": name, "value": value})

    mock_custom_events.save = fake_save

    class MockAgent:
        async def arun(self, *args, **kwargs):
            return MagicMock()

    runtime = AgentRuntime(agent=MockAgent())
    runtime.stores = Stores(custom_events=mock_custom_events)

    async def fake_resume(entity, session_id, tool_messages, run_context=None, run_kwargs=None):
        async def stream():
            yield content("Approval received, database deleted.")
            yield run_completed()

        return stream()

    monkeypatch.setattr("agno_harness.runtime.runner.resume_paused_run", fake_resume)

    app = RelayApp(
        runtime,
        action_store=action_store,
        session_manager=None,
    )
    app.session_manager.store = session_store

    class DummyChannel:
        name = "teams"

        async def ack(self, event, emoji="👀"):
            return "ack-tok"

        async def typing(self, key, active):
            pass

        async def send(self, key, message):
            self.last_msg = message
            return "m-out"

        async def settle(self, key, token, emoji="✅"):
            self.settled_emoji = emoji

    dummy = DummyChannel()
    key = ConversationKey(
        platform="teams", chat_id="chat-hitl", thread_id="thread-1", sender_id="alice"
    )

    # 1. Pre-create pending action record in DB
    await action_store.create_action(
        action_id="agno.hitl.resume",
        session_key=key.session_key,
        agno_session_id="session-hitl-1",
        payload={"tool_call_id": "call_drop_table"},
        tool_call_id="call_drop_table",
        meta={"environment": "production"},
    )

    # 2. Simulate user clicking Approve button on Teams
    click_event = ChannelEvent(
        event_id="btn-click-1",
        key=key,
        text="",
        action_id="agno.hitl.resume",
        action_value={
            "tool_call_id": "call_drop_table",
            "pause_type": "confirmation",
            "accepted": True,
            "session_id": "session-hitl-1",
            "meta": {"environment": "production"},
        },
    )

    result = await app.handle_event(dummy, click_event)

    # 3. Verify action status updated in DB
    act_record = await action_store.get_action(tool_call_id="call_drop_table")
    assert act_record is not None
    assert act_record["status"] == "approved"

    # 4. Verify CUSTOM frame action.resolved was emitted for Web UI / history replay
    assert len(saved_custom_frames) == 1
    custom_frame = saved_custom_frames[0]
    assert custom_frame["name"] == "action.resolved"
    assert custom_frame["value"]["actionId"] == "agno.hitl.resume"
    assert custom_frame["value"]["toolCallId"] == "call_drop_table"
    assert custom_frame["value"]["decision"] == "accepted"
    assert custom_frame["value"]["userId"] == "alice"
    assert custom_frame["value"]["meta"]["environment"] == "production"

    # 5. Verify agent turn resumed and channel output sent
    assert "Approval received, database deleted." in result.text
    assert dummy.settled_emoji == "✅"


def test_redis_run_event_log_lazy_export():
    import agno_harness

    redis_cls = agno_harness.RedisRunEventLog
    assert redis_cls.__name__ == "RedisRunEventLog"
    assert hasattr(redis_cls, "from_url")
