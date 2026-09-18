import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import Request
from starlette.testclient import TestClient

from agno_harness.app import RelayApp
from agno_harness.core.channel import ChannelEvent, OutboundMessage
from agno_harness.runtime.runtime import AgentRuntime
from agno_harness.server import RelayServer
from agno_harness.sessions.manager import SQLiteSessionStore
from agno_harness.sessions.models import ConversationKey, SessionRecord


class MockAgent:
    name = "MockAgent"

    async def arun(self, *args, **kwargs):
        return MagicMock()


@pytest.fixture
def mock_runtime():
    agent = MockAgent()
    return AgentRuntime(agent=agent)


def test_relay_server_fail_fast_missing_auth(mock_runtime):
    from agno_harness.transport.router import ConfigurationError

    relay = RelayApp(mock_runtime)
    with pytest.raises(
        ConfigurationError, match="RelayServer requires user authentication in production"
    ):
        RelayServer(relay)


def test_relay_server_basic_and_health(mock_runtime):
    relay = RelayApp(mock_runtime)
    server = RelayServer(relay, allow_anonymous=True)

    client = TestClient(server)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "agno-harness" in data["title"]


def test_relay_server_subclassing_and_user_resolver(mock_runtime):
    relay = RelayApp(mock_runtime)

    class CustomServer(RelayServer):
        def resolve_user_id(self, request: Request) -> str | None:
            return request.headers.get("X-Custom-User", "anonymous")

        def setup_routes(self) -> None:
            super().setup_routes()

            @self.get("/api/custom")
            async def custom_endpoint():
                return {"custom": True}

    server = CustomServer(relay)
    client = TestClient(server)

    # Check custom route works
    custom_resp = client.get("/api/custom")
    assert custom_resp.status_code == 200
    assert custom_resp.json() == {"custom": True}

    # Verify user resolver hook
    req = Request({"type": "http", "headers": [(b"x-custom-user", b"user-42")]})
    assert server.resolve_user_id(req) == "user-42"


@pytest.mark.asyncio
async def test_relay_app_action_registration():
    agent = MockAgent()
    runtime = AgentRuntime(agent=agent)
    app = RelayApp(runtime)

    action_received = []

    @app.action("btn_approve")
    async def handle_approve(event: ChannelEvent) -> OutboundMessage:
        action_received.append(event.action_id)
        return OutboundMessage(text="Order approved successfully!")

    class DummyChannel:
        name = "mock_channel"

        async def ack(self, event, emoji="👀"):
            return "ack-token"

        async def typing(self, key, active):
            pass

        async def send(self, key, message):
            self.last_sent = message
            return "m1"

        async def settle(self, key, token, emoji="✅"):
            pass

    dummy = DummyChannel()
    key = ConversationKey(platform="teams", chat_id="chat-1", thread_id="thread-1")
    event = ChannelEvent(
        event_id="evt-1",
        key=key,
        text="",
        action_id="btn_approve",
        action_value={"order_id": "123"},
    )

    result = await app.handle_event(dummy, event)
    assert action_received == ["btn_approve"]
    assert "approved successfully" in result.text


@pytest.mark.asyncio
async def test_relay_app_agno_hitl_resume(monkeypatch):
    class ResumeMockAgent:
        async def arun(self, *args, **kwargs):
            async def stream():
                yield "Resumed turn finished"

            return stream()

    runtime = AgentRuntime(agent=ResumeMockAgent())
    app = RelayApp(runtime)

    resumed_tools = []

    from tests.conftest import content, run_completed

    async def fake_resume(entity, session_id, tool_messages, run_context=None, run_kwargs=None):
        resumed_tools.extend(tool_messages)

        async def stream():
            yield content("Resumed execution after approval")
            yield run_completed()

        return stream()

    monkeypatch.setattr("agno_harness.runtime.runner.resume_paused_run", fake_resume)

    class DummyChannel:
        name = "teams"

        async def ack(self, event, emoji="👀"):
            return "ack-token"

        async def typing(self, key, active):
            pass

        async def send(self, key, message):
            self.last_sent = message
            return "m1"

        async def settle(self, key, token, emoji="✅"):
            pass

    dummy = DummyChannel()
    key = ConversationKey(platform="teams", chat_id="chat-1", thread_id="thread-1")
    event = ChannelEvent(
        event_id="evt-hitl-1",
        key=key,
        text="",
        action_id="agno.hitl.resume",
        action_value={
            "tool_call_id": "call_delete_db",
            "pause_type": "confirmation",
            "accepted": True,
        },
    )

    result = await app.handle_event(dummy, event)
    assert len(resumed_tools) == 1
    assert resumed_tools[0].tool_call_id == "call_delete_db"
    assert json.loads(resumed_tools[0].content)["accepted"] is True
    assert "Resumed execution after approval" in result.text


@pytest.mark.asyncio
async def test_sqlite_session_store(tmp_path):
    db_file = str(tmp_path / "test_sessions.db")
    store = SQLiteSessionStore(db_file)

    key = "chat-1:user-1"
    record = SessionRecord(
        session_key=key,
        agno_session_id="session-xyz",
        metadata={"user": "alice"},
    )

    await store.save(record)

    loaded = await store.get(key)
    assert loaded is not None
    assert loaded.session_key == key
    assert loaded.agno_session_id == "session-xyz"
    assert loaded.metadata.get("user") == "alice"

    # Non-existent session
    missing = await store.get("non-existent")
    assert missing is None


@pytest.mark.asyncio
async def test_in_memory_action_store_and_stores():
    from agno_harness import InMemoryActionStore, Stores

    action_store = InMemoryActionStore()
    ref = await action_store.create_action(
        action_id="like_card",
        session_key="teams:c1:u1",
        agno_session_id="s1",
        payload={"message_id": "m-123"},
        meta={"source": "card"},
    )
    assert ref is not None

    record = await action_store.get_action(session_key="teams:c1:u1")
    assert record is not None
    assert record["action_id"] == "like_card"
    assert record["status"] == "pending"

    await action_store.update_status("like_card", "completed", result={"voted": True})
    updated = await action_store.get_action(session_key="teams:c1:u1")
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated["result"]["voted"] is True

    # Test Stores.in_memory()
    stores = Stores.in_memory()
    assert stores.resume_mode.value == "live"
    assert stores.custom_events is not None
    assert stores.history_archive is not None


@pytest.mark.asyncio
async def test_standalone_silent_action_no_llm():
    from agno_harness import AgentRuntime, ConversationKey, OutboundMessage, RelayApp

    class UnusedMockAgent:
        async def arun(self, *args, **kwargs):
            raise RuntimeError("LLM should NOT be called for standalone action!")

    runtime = AgentRuntime(agent=UnusedMockAgent())
    app = RelayApp(runtime)

    like_called = False

    @app.action("thumbs_up")
    async def handle_thumbs_up(event: ChannelEvent) -> None:
        nonlocal like_called
        like_called = True
        # Return None: silent action, no spam chat bubble

    class MockChannel:
        name = "teams"
        settled_emoji = None
        sent_messages: list[Any] = []

        async def ack(self, event, emoji="👀"):
            return "ack-1"

        async def send(self, key, message):
            self.sent_messages.append(message)
            return "msg-id"

        async def settle(self, key, token, emoji="✅"):
            self.settled_emoji = emoji

    channel = MockChannel()
    key = ConversationKey(platform="teams", chat_id="c1", sender_id="alice")
    event = ChannelEvent(
        event_id="e-vote-1",
        key=key,
        text="",
        action_id="thumbs_up",
        action_value={"rating": 5},
    )

    outbound = await app.handle_event(channel, event)
    assert like_called is True
    # Verify no message sent to channel (no spam)
    assert len(channel.sent_messages) == 0
    assert channel.settled_emoji == "✅"
    assert outbound.text == ""

    # Test custom settle emoji
    @app.action("heart")
    async def handle_heart(event: ChannelEvent) -> OutboundMessage:
        return OutboundMessage(text="", extra={"settle_emoji": "❤️"})

    event_heart = ChannelEvent(
        event_id="e-heart-1",
        key=key,
        text="",
        action_id="heart",
    )
    await app.handle_event(channel, event_heart)
    assert channel.settled_emoji == "❤️"
    assert len(channel.sent_messages) == 0


@pytest.mark.asyncio
async def test_unhandled_action_does_not_invoke_agent():
    """Unknown action_id must not fall through into a normal agent turn."""

    class UnusedMockAgent:
        async def arun(self, *args, **kwargs):
            raise RuntimeError("LLM should NOT be called for unhandled actions!")

    class FakeCatalog:
        async def dispatch_action(self, action_id, payload, event):
            return None  # not ours

    runtime = AgentRuntime(agent=UnusedMockAgent())
    app = RelayApp(runtime, card_catalog=FakeCatalog())

    class MockChannel:
        name = "teams"
        settled_emoji = None
        sent_messages: list[Any] = []
        ack_count = 0

        async def ack(self, event, emoji="👀"):
            self.ack_count += 1
            return "ack-unhandled"

        async def send(self, key, message):
            self.sent_messages.append(message)
            return "msg-id"

        async def settle(self, key, token, emoji="✅"):
            self.settled_emoji = emoji
            self.settled_token = token

        async def typing(self, key, active):
            pass

    channel = MockChannel()
    key = ConversationKey(platform="teams", chat_id="c1", sender_id="alice")
    event = ChannelEvent(
        event_id="e-unknown-1",
        key=key,
        text="",
        action_id="totally.unknown.action",
        action_value={"x": 1},
    )

    outbound = await app.handle_event(channel, event)
    assert outbound is not None
    assert outbound.text == ""
    assert len(channel.sent_messages) == 0
    assert channel.ack_count == 1
    assert channel.settled_emoji == "❓"
    assert channel.settled_token == "ack-unhandled"


@pytest.mark.asyncio
async def test_in_memory_action_store_update_status_scoped_by_tool_call_id():
    """HITL shares action_id; update_status must only touch the clicked tool_call_id."""
    from agno_harness import InMemoryActionStore

    store = InMemoryActionStore()
    await store.create_action(
        action_id="agno.hitl.resume",
        session_key="teams:c1:u1",
        agno_session_id="s1",
        payload={},
        tool_call_id="call_a",
    )
    await store.create_action(
        action_id="agno.hitl.resume",
        session_key="teams:c1:u1",
        agno_session_id="s1",
        payload={},
        tool_call_id="call_b",
    )

    await store.update_status(
        action_id="agno.hitl.resume",
        status="approved",
        tool_call_id="call_a",
        result={"accepted": True},
    )

    row_a = await store.get_action(tool_call_id="call_a")
    row_b = await store.get_action(tool_call_id="call_b")
    assert row_a is not None and row_a["status"] == "approved"
    assert row_a["result"]["accepted"] is True
    assert row_b is not None and row_b["status"] == "pending"
    assert row_b["result"] == {}


@pytest.mark.asyncio
async def test_postgres_sink_missing_dependency(monkeypatch):
    import sys

    from agno_harness.sinks import PostgresSink

    # The extra may already be installed; hide it so this path stays unit-tested.
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    sink = PostgresSink("postgresql://localhost/fake")
    with pytest.raises(ImportError, match="PostgresSink requires the 'asyncpg' package"):
        await sink.record_inbound(
            ChannelEvent(
                event_id="evt-1",
                key=ConversationKey(platform="web", chat_id="c1"),
                text="hello",
            ),
            "s1",
        )
