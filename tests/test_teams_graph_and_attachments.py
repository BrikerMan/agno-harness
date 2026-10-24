"""Unit tests for Teams Graph client, reference attachment enrichment, and attachment fallback."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import httpx
import pytest

from agno_harness import AgentRuntime, RelayApp
from agno_harness.channels.teams import TeamsChannel
from agno_harness.channels.teams.graph import (
    TeamsGraphClient,
    encode_sharing_url,
    extract_html_file_attachments,
)
from agno_harness.core.attachment import InboundAttachment
from agno_harness.core.channel import ChannelEvent
from agno_harness.sessions.models import ConversationKey

from .conftest import FakeAgent, run_completed


def test_encode_sharing_url():
    url = "https://yodo1net-my.sharepoint.com/personal/user/file.docx"
    encoded = encode_sharing_url(url)
    assert encoded.startswith("u!")
    raw_b64 = encoded[2:]
    padding = "=" * ((4 - len(raw_b64) % 4) % 4)
    decoded = base64.urlsafe_b64decode(raw_b64 + padding).decode("utf-8")
    assert decoded == url


def test_extract_html_file_attachments():
    html = (
        '<p>Please check <a href="https://yodo1net-my.sharepoint.com/doc.docx" '
        'itemtype="http://schema.skype.com/HyperLink/Files">Press Release.docx</a></p>'
        '<p>And link <a href="https://example.com/page">webpage</a></p>'
    )
    atts = extract_html_file_attachments(html)
    assert len(atts) == 1
    assert atts[0].name == "Press Release.docx"
    assert atts[0].url == "https://yodo1net-my.sharepoint.com/doc.docx"
    assert atts[0].content_type == "reference"


@pytest.mark.asyncio
async def test_teams_graph_token_and_caching():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth2/v2.0/token")
        return httpx.Response(200, json={"access_token": "mock-token", "expires_in": 3600})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    graph = TeamsGraphClient("test-app", "test-pass", "test-tenant", http=client)

    token1 = await graph.get_token()
    assert token1 == "mock-token"

    # Second call uses cached token
    token2 = await graph.get_token()
    assert token2 == "mock-token"
    await graph.aclose()


@pytest.mark.asyncio
async def test_teams_graph_fetch_message_attachments_chat():
    graph_msg = {
        "id": "1792838623956",
        "attachments": [
            {
                "id": "att-1",
                "contentType": "reference",
                "name": "release.docx",
                "contentUrl": "https://yodo1net-my.sharepoint.com/release.docx",
            },
            {
                "id": "card-1",
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {},
            },
        ],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "token-xyz"})
        if "messages/1792838623956" in request.url.path:
            return httpx.Response(200, json=graph_msg, headers={"content-type": "application/json"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    graph = TeamsGraphClient("app", "pass", "tenant", http=client)

    activity = {
        "id": "1792838623956",
        "conversation": {"id": "19:group_chat;messageid=1792838623956"},
    }
    atts = await graph.fetch_message_attachments(activity)
    assert len(atts) == 1
    assert atts[0].id == "att-1"
    assert atts[0].name == "release.docx"
    assert atts[0].url == "https://yodo1net-my.sharepoint.com/release.docx"
    assert atts[0].content_type == "reference"
    await graph.aclose()


@pytest.mark.asyncio
async def test_teams_graph_download_share_success_and_denial():
    calls = []

    denied_token = encode_sharing_url("https://sharepoint.com/denied.docx")

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "token-xyz"})
        if "/driveItem/content" in request.url.path:
            if denied_token in request.url.path:
                return httpx.Response(403, text="Forbidden")
            return httpx.Response(200, content=b"PK\x03\x04docx-bytes")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    graph = TeamsGraphClient("app", "pass", "tenant", http=client)

    # Success
    data = await graph.download_share("https://sharepoint.com/file.docx")
    assert data == b"PK\x03\x04docx-bytes"

    # Permission denied returns None
    denied = await graph.download_share("https://sharepoint.com/denied.docx")
    assert denied is None
    await graph.aclose()


@pytest.mark.asyncio
async def test_teams_channel_enrichment_and_fetch():
    graph_msg = {
        "id": "msg-123",
        "attachments": [
            {
                "id": "att-1",
                "name": "brief.docx",
                "contentType": "reference",
                "contentUrl": "https://sharepoint.com/brief.docx",
            }
        ],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "token-xyz"})
        if "/chats/" in request.url.path:
            return httpx.Response(200, json=graph_msg, headers={"content-type": "application/json"})
        if "/driveItem/content" in request.url.path:
            return httpx.Response(200, content=b"file-content")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)

    channel = TeamsChannel(bot_app_id="app", bot_app_password="pass", tenant_id="tenant")
    channel.graph._http = http

    activity = {
        "id": "msg-123",
        "type": "message",
        "conversation": {"id": "19:chat-1"},
        "from": {"id": "user-1", "name": "Alice"},
        "text": "",  # Empty text
    }

    event = channel._event_from_activity(activity)
    await channel._enrich_event_attachments(event, activity)

    assert len(event.attachments) == 1
    assert event.attachments[0].name == "brief.docx"
    assert event.text == "[附件: brief.docx]"

    data = await channel.fetch_attachment(event, event.attachments[0])
    assert data == b"file-content"


@pytest.mark.asyncio
async def test_app_attachment_fallback_informs_agent():
    """Requirement 2: Even without processor or permission, agent must know file is attached."""
    agent = FakeAgent([run_completed("I acknowledge the file.")])
    runtime = AgentRuntime(agent=agent)
    relay = RelayApp(runtime=runtime, chime_in_policy=None)

    channel = TeamsChannel(bot_app_id="app", bot_app_password="pass")
    event = ChannelEvent(
        event_id="evt-1",
        key=ConversationKey(platform="teams", chat_id="19:test", is_direct_message=True),
        text="What is this?",
        attachments=[
            InboundAttachment(
                id="att-1",
                name="Q3_Report.docx",
                content_type="reference",
                url="https://sharepoint.com/report.docx",
                data=None,  # No download / permission denied
            )
        ],
    )

    channel.send = AsyncMock(return_value="sent-1")
    channel.settle = AsyncMock()

    await relay.handle_event(channel, event)

    # Check agent received prompt with attachment note
    input_text = agent.last_kwargs["input"]
    assert "Q3_Report.docx" in input_text
    assert "file is attached" in input_text
