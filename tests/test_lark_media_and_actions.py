"""Lark images reach the agent as media; card buttons become action events."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from ag_ui.core import RunAgentInput, UserMessage
from ag_ui.core.types import ImageInputContent, InputContentDataSource, TextInputContent

from agno_harness import AgentRuntime
from agno_harness.app import _user_content
from agno_harness.channels.lark import LarkChannel, _post_image_keys
from agno_harness.core.attachment import InboundAttachment
from agno_harness.runtime.replay import last_user_text
from tests.conftest import FakeAgent, content, run_completed

PNG = b"\x89PNG\r\n\x1a\nfake"


def test_post_image_keys_plain_and_localized():
    plain = {
        "title": "",
        "content": [[{"tag": "img", "image_key": "img_a"}, {"tag": "text", "text": "x"}]],
    }
    localized = {"zh_cn": {"title": "", "content": [[{"tag": "img", "image_key": "img_b"}]]}}
    assert _post_image_keys(plain) == ["img_a"]
    assert _post_image_keys(localized) == ["img_b"]
    assert _post_image_keys({"content": [[{"tag": "text", "text": "no image"}]]}) == []


def test_user_content_is_text_without_downloaded_images():
    pending = InboundAttachment(id="img_a", content_type="image/jpeg")
    assert _user_content("hi", [pending]) == "hi"


def test_user_content_adds_image_parts():
    image = InboundAttachment(id="img_a", content_type="image/png", data=PNG)
    parts = _user_content("法兰渗漏", [image])
    assert isinstance(parts, list)
    assert parts[0].text == "法兰渗漏"
    assert parts[1].source.value == base64.b64encode(PNG).decode("ascii")
    assert parts[1].source.mime_type == "image/png"


def test_last_user_text_ignores_media_parts():
    msg = UserMessage(
        id="u1",
        role="user",
        content=[
            TextInputContent(text="看这张"),
            ImageInputContent(source=InputContentDataSource(value="QUJD", mime_type="image/png")),
        ],
    )
    assert last_user_text([msg]) == "看这张"


@pytest.mark.asyncio
async def test_runtime_keeps_images_through_query_envelope():
    agent = FakeAgent([content("ok"), run_completed()])
    runtime = AgentRuntime(agent=agent)
    image = InboundAttachment(id="img_a", content_type="image/png", data=PNG)
    run_input = RunAgentInput(
        thread_id="t-img",
        run_id="r-img",
        state={},
        messages=[UserMessage(id="u1", role="user", content=_user_content("看这张", [image]))],
        tools=[],
        context=[],
        forwarded_props=None,
    )
    async for _ in runtime.stream_events(run_input):
        pass
    assert agent.last_kwargs["input"].startswith("<user-query>\n看这张\n</user-query>")
    images = agent.last_kwargs["images"]
    assert images and len(images) == 1
    assert "QUJD" not in agent.last_kwargs["input"]


@pytest.mark.asyncio
async def test_card_action_becomes_channel_event():
    channel = LarkChannel(app_id="cli", app_secret="sec", use_websocket=False)
    await channel.start()
    data = SimpleNamespace(
        event=SimpleNamespace(
            token="tok-1",
            action=SimpleNamespace(value={"action_id": "hazard.accurate", "inspection_id": 7}),
            context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
            operator=SimpleNamespace(open_id="ou_1"),
        )
    )
    channel._handle_card_action(data)
    event = await channel._inbound_queue.get()
    assert event.action_id == "hazard.accurate"
    assert event.action_value == {"action_id": "hazard.accurate", "inspection_id": 7}
    assert event.key.reply_to_id == "om_1"
    assert event.key.chat_id == "oc_1"
    assert event.key.sender_id == "ou_1"
    await channel.stop()


@pytest.mark.asyncio
async def test_card_action_without_action_id_is_dropped():
    channel = LarkChannel(app_id="cli", app_secret="sec", use_websocket=False)
    await channel.start()
    data = SimpleNamespace(
        event=SimpleNamespace(
            token="tok-2",
            action=SimpleNamespace(value={"foo": 1}),
            context=SimpleNamespace(open_message_id="om_2", open_chat_id="oc_2"),
            operator=SimpleNamespace(open_id="ou_2"),
        )
    )
    channel._handle_card_action(data)
    assert channel._inbound_queue.empty()
    await channel.stop()
