from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agno_harness.app import RelayApp
from agno_harness.runtime.runtime import AgentRuntime
from agno_harness.sessions.models import ConversationKey
from agno_harness.transport.router import ConfigurationError, make_relay_router

from .conftest import FakeAgent, content, make_input, run_completed


@pytest.fixture
def mock_runtime():
    return AgentRuntime(agent=FakeAgent([content("hi"), run_completed()]))


def test_missing_resolve_user_id_raises_configuration_error(mock_runtime):
    relay = RelayApp(mock_runtime)

    # In production without allow_anonymous=True, must raise ConfigurationError
    with pytest.raises(ConfigurationError, match="resolve_user_id is required in production"):
        make_relay_router(relay)

    with pytest.raises(ConfigurationError):
        relay.get_router()


def test_mount_relay_router_into_external_fastapi(mock_runtime):
    relay = RelayApp(mock_runtime)

    def my_auth(req: Request) -> str | None:
        token = req.headers.get("Authorization")
        if token == "Bearer secret-token":
            return "user-999"
        return None

    # Developer's existing enterprise FastAPI app
    app = FastAPI(title="My Corporate Microservice")

    # Add custom enterprise endpoints
    @app.get("/api/v1/orders")
    def get_orders():
        return [{"order_id": 101, "total": 99.5}]

    # Mount Relay router directly
    router = relay.get_router(prefix="/agent-relay", resolve_user_id=my_auth)
    app.include_router(router)

    client = TestClient(app)

    # 1. Custom app endpoint works
    resp = client.get("/api/v1/orders")
    assert resp.status_code == 200
    assert resp.json() == [{"order_id": 101, "total": 99.5}]

    # 2. Health check mounted under prefix
    health_resp = client.get("/agent-relay/api/v1/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "healthy"
    assert health_resp.json()["resumeMode"] == "none"

    payload = make_input().model_dump(by_alias=True, mode="json")

    # 3. Unauthenticated request to /api/v1/channels/web/agui gets HTTP 401
    post_resp = client.post(
        "/agent-relay/api/v1/channels/web/agui",
        json=payload,
    )
    assert post_resp.status_code == 401
    assert "Authentication required" in post_resp.json()["detail"]

    # 4. Authenticated request is accepted
    auth_resp = client.post(
        "/agent-relay/api/v1/channels/web/agui",
        headers={"Authorization": "Bearer secret-token"},
        json=payload,
    )
    # 200 OK SSE streaming
    assert auth_resp.status_code == 200
    assert "text/event-stream" in auth_resp.headers["content-type"]


def test_allow_anonymous_for_local_dev(mock_runtime):
    relay = RelayApp(mock_runtime)
    app = FastAPI()
    router = make_relay_router(relay, allow_anonymous=True)
    app.include_router(router)

    client = TestClient(app)
    payload = make_input().model_dump(by_alias=True, mode="json")
    resp = client.post(
        "/api/v1/channels/web/agui",
        json=payload,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_user_defined_session_resolver(mock_runtime):
    # Topology: Shared channel thread where multiple users collaborate in one context
    def custom_resolver(key):
        return f"{key.platform}:{key.chat_id}:{key.thread_id}"

    relay = RelayApp(mock_runtime, session_resolver=custom_resolver)

    user_a_key = ConversationKey(
        platform="teams",
        chat_id="channel-123",
        thread_id="topic-456",
        sender_id="alice",
    )
    user_b_key = ConversationKey(
        platform="teams",
        chat_id="channel-123",
        thread_id="topic-456",
        sender_id="bob",
    )

    # Both users in the same topic resolve to the identical agno session id
    sess_a, is_new_a = await relay.session_manager.get_or_create_session(user_a_key)
    sess_b, is_new_b = await relay.session_manager.get_or_create_session(user_b_key)

    assert is_new_a is True
    assert is_new_b is False
    assert sess_a == sess_b


@pytest.mark.asyncio
async def test_default_session_resolver_isolates_users_in_thread(mock_runtime):
    relay = RelayApp(mock_runtime)

    user_a_key = ConversationKey(
        platform="teams",
        chat_id="channel-123",
        thread_id="topic-456",
        sender_id="alice",
    )
    user_b_key = ConversationKey(
        platform="teams",
        chat_id="channel-123",
        thread_id="topic-456",
        sender_id="bob",
    )

    # Default topology: per-person isolation within thread
    sess_a, _ = await relay.session_manager.get_or_create_session(user_a_key)
    sess_b, _ = await relay.session_manager.get_or_create_session(user_b_key)

    assert sess_a != sess_b


@pytest.mark.asyncio
async def test_webhook_deduplication(mock_runtime):
    from agno_harness.channels.base import BaseChannel
    from agno_harness.core.channel import ChannelEvent

    channel = BaseChannel(name="test_chan")
    channel.send = AsyncMock()
    channel.ack = AsyncMock(return_value="token-1")
    channel.typing = AsyncMock()
    channel.settle = AsyncMock()

    relay = RelayApp(mock_runtime)

    event = ChannelEvent(
        channel_name="test_chan",
        event_id="evt-duplicate-101",
        key=ConversationKey(platform="test", chat_id="c1", sender_id="u1"),
        text="Hello agent",
    )

    # First event handled normally
    out_1 = await relay.handle_event(channel, event)
    assert out_1.text != ""
    assert channel.send.call_count == 1

    # Immediate duplicate event dropped
    out_2 = await relay.handle_event(channel, event)
    assert out_2.text == ""
    # send should not be called again
    assert channel.send.call_count == 1


@pytest.mark.asyncio
async def test_fastapi_lifespan_integration(mock_runtime):
    relay = RelayApp(mock_runtime)
    app = FastAPI(lifespan=relay.lifespan)
    app.include_router(relay.get_router(allow_anonymous=True))

    with TestClient(app) as client:
        # relay._running should be True during lifespan
        assert relay._running is True
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    # after context exit, relay._running should be False
    assert relay._running is False


def test_web_channel_does_not_conflict_with_make_relay_router(mock_runtime):
    from agno_harness.channels.web import WebChannel

    relay = RelayApp(mock_runtime)
    relay.add_channel(WebChannel())

    app = FastAPI()
    router = relay.get_router(allow_anonymous=True)
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert "web" in resp.json()["channels"]
    assert resp.json()["resumeMode"] == "none"


def test_health_resume_mode_is_live_when_long_runs_can_follow(mock_runtime):
    from agno_harness.runtime.longrun import LongRunManager
    from agno_harness.stores import InMemoryRunEventLog

    log = InMemoryRunEventLog()
    long_runs = LongRunManager(mock_runtime, log=log)
    app = FastAPI()
    app.include_router(make_relay_router(mock_runtime, allow_anonymous=True, long_runs=long_runs))
    body = TestClient(app).get("/api/v1/health").json()
    assert body["resumeMode"] == "live"
