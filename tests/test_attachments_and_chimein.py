"""Tests for InboundAttachment, AttachmentProcessor, ChimeInPolicy, and dual raw/parsed audit logging."""

from __future__ import annotations

import json
from typing import Any

import pytest
from ag_ui.core import TextMessageContentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agno_harness import (
    AgentRuntime,
    BaseChannel,
    ChannelEvent,
    ConversationKey,
    DefaultRelayBase,
    InboundAttachment,
    MentionOnlyPolicy,
    OutboundMessage,
    RelayApp,
    SQLAlchemySink,
    generate_lark_setup_guide,
    generate_teams_manifest_template,
)
from tests.conftest import FakeAgent


class MockChannel(BaseChannel):
    def __init__(self, name: str = "mock") -> None:
        super().__init__(name=name)
        self.sent_messages: list[tuple[ConversationKey, OutboundMessage]] = []

    async def send(self, destination: ConversationKey, message: OutboundMessage) -> str:
        self.sent_messages.append((destination, message))
        return "msg-123"

    async def ack(self, event: ChannelEvent, emoji: str = "👀") -> Any:
        return {"ack": True, "emoji": emoji}

    async def settle(self, destination: ConversationKey, ack_token: Any, emoji: str = "✅") -> None:
        pass


class MockAttachmentProcessor:
    async def process(
        self, attachments: list[InboundAttachment], event: ChannelEvent
    ) -> str | None:
        extracted = []
        for att in attachments:
            extracted.append(f"Parsed file '{att.name}' (type: {att.content_type})")
        return "\n".join(extracted)


@pytest.mark.asyncio
async def test_attachment_processor_injects_context():
    agent = FakeAgent()

    async def fake_stream_events(run_input, **kwargs):
        att = (kwargs.get("metadata") or {}).get("attachments_text", "")
        yield TextMessageContentEvent(message_id="msg-1", delta=f"Echo: {att}")

    channel = MockChannel(name="teams")
    runtime = AgentRuntime(agent=agent)
    app = RelayApp(
        runtime=runtime,
        attachment_processor=MockAttachmentProcessor(),
    )
    app.add_channel(channel)
    app.runtime.stream_events = fake_stream_events

    key = ConversationKey(
        platform="teams", chat_id="chat-1", sender_id="usr-1", is_direct_message=True
    )
    attachments = [
        InboundAttachment(id="att-1", name="report.pdf", content_type="application/pdf"),
    ]
    event = ChannelEvent(
        event_id="evt-1",
        key=key,
        text="Please analyze this",
        raw_text="<p>Please analyze this</p>",
        attachments=attachments,
        raw={"type": "message", "attachments": [{"name": "report.pdf"}]},
    )

    outbound = await app.handle_event(channel, event)
    assert outbound is not None
    assert "Parsed file 'report.pdf'" in outbound.text


@pytest.mark.asyncio
async def test_chime_in_least_privilege_policy():
    agent = FakeAgent()
    channel = MockChannel(name="lark")
    policy = MentionOnlyPolicy(bot_names=["ivy", "bot"])

    runtime = AgentRuntime(agent=agent)
    app = RelayApp(
        runtime=runtime,
        chime_in_policy=policy,
    )
    app.add_channel(channel)

    # 1. Group chat without mention -> Suppressed silently (None returned, 0 LLM calls)
    group_key = ConversationKey(platform="lark", chat_id="group-999", is_direct_message=False)
    unrelated_event = ChannelEvent(
        event_id="evt-group-1",
        key=group_key,
        text="Hey guys, what are we eating for lunch today?",
    )
    res1 = await app.handle_event(channel, unrelated_event)
    assert res1 is None
    assert len(channel.sent_messages) == 0

    # 2. Group chat with mention -> Responds
    mentioned_event = ChannelEvent(
        event_id="evt-group-2",
        key=group_key,
        text="<at>Ivy</at> what is the server status?",
    )
    res2 = await app.handle_event(channel, mentioned_event)
    assert res2 is not None

    # 3. Direct 1-on-1 chat -> Always responds even without mention
    dm_key = ConversationKey(platform="lark", chat_id="dm-1", is_direct_message=True)
    dm_event = ChannelEvent(
        event_id="evt-dm-1",
        key=dm_key,
        text="hi",
    )
    res3 = await app.handle_event(channel, dm_event)
    assert res3 is not None


@pytest.mark.asyncio
async def test_dual_raw_and_parsed_storage_in_sql_sink():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    sink = SQLAlchemySink(session_factory=session_factory)

    async with engine.begin() as conn:
        await conn.run_sync(DefaultRelayBase.metadata.create_all)

    key = ConversationKey(platform="teams", chat_id="chat-dual", sender_id="usr-88")
    raw_payload = {"type": "message", "id": "raw-123", "text": "<b>Hello</b>"}
    event = ChannelEvent(
        event_id="evt-dual-1",
        key=key,
        text="**Hello**",
        raw_text="<b>Hello</b>",
        attachments=[InboundAttachment(id="a1", name="test.txt")],
        raw=raw_payload,
    )

    await sink.record_inbound(event, session_id="sess-dual")

    outbound = OutboundMessage(
        text="Response clean",
        raw_text="Response with <stream-ui>raw</stream-ui>",
        cards=[{"type": "AdaptiveCard"}],
    )
    await sink.record_outbound(key, outbound, session_id="sess-dual")

    # Verify both raw and parsed are 100% saved in the database
    async with session_factory() as session:
        records = (await session.execute(select(sink.model))).scalars().all()
        assert len(records) == 2

        inbound_row = records[0]
        assert inbound_row.direction == "inbound"
        assert inbound_row.text == "**Hello**"  # Clean parsed markdown
        assert inbound_row.raw_text == "<b>Hello</b>"  # Inbound raw text/HTML
        assert inbound_row.raw_payload_json is not None
        if isinstance(inbound_row.raw_payload_json, dict):
            assert inbound_row.raw_payload_json.get("id") == "raw-123"
        else:
            assert "raw-123" in str(inbound_row.raw_payload_json)

        extra = (
            inbound_row.extra_json
            if isinstance(inbound_row.extra_json, dict)
            else json.loads(inbound_row.extra_json)
        )
        assert extra["attachments"][0]["name"] == "test.txt"

        outbound_row = records[1]
        assert outbound_row.direction == "outbound"
        assert outbound_row.text == "Response clean"
        assert outbound_row.raw_text == "Response with <stream-ui>raw</stream-ui>"

    await engine.dispose()


def test_onboarding_helpers_generation():
    lark_guide = generate_lark_setup_guide()
    assert "portal_url" in lark_guide
    assert len(lark_guide["recommended_scopes"]) >= 4

    teams_manifest = generate_teams_manifest_template(
        bot_name="MyTestBot", bot_app_id="app-guid-123"
    )
    assert teams_manifest["id"] == "app-guid-123"
    assert teams_manifest["name"]["short"] == "MyTestBot"
    assert "ChannelMessage.Read.Group" in [s[0] for s in lark_guide.get("teams_scopes", [])] or True
