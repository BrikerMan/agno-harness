"""Bot Connector replies stay successful when the body omits an activity id."""

import time

import httpx
import pytest

from agno_harness.channels.teams_connector import TeamsConnectorClient, TeamsConversationRef

_SERVICE = "https://smba.trafficmanager.net/amer"


def _ref() -> TeamsConversationRef:
    return TeamsConversationRef(
        service_url=_SERVICE,
        conversation_id="19:chat",
        activity_id="inbound-1",
        bot_id="bot",
        bot_name="Bot",
        user_id="user",
        user_name="User",
    )


def _client(handler) -> TeamsConnectorClient:
    client = TeamsConnectorClient(
        "app-id",
        "secret",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client._token = "token"
    client._token_deadline = time.monotonic() + 60
    return client


@pytest.mark.asyncio
async def test_post_activity_reads_id_from_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "activity-9"})

    activity_id = await _client(handler).post_activity(
        _ref(), {"type": "message", "text": "hi"}, reply=True
    )
    assert activity_id == "activity-9"


@pytest.mark.asyncio
async def test_post_activity_reads_id_from_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers={"Location": f"{_SERVICE}/v3/conversations/19:chat/activities/from-location"},
        )

    activity_id = await _client(handler).post_activity(
        _ref(), {"type": "message", "text": "hi"}, reply=True
    )
    assert activity_id == "from-location"


@pytest.mark.asyncio
async def test_post_activity_accepts_success_without_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    activity_id = await _client(handler).post_activity(
        _ref(), {"type": "message", "text": "hi"}, reply=True
    )
    assert activity_id == ""
