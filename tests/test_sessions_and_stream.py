import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from ag_ui.core import CustomEvent, TextMessageContentEvent

from agno_relay.core.channel import ConversationKey
from agno_relay.core.streamui.schema import BlockSchema, CardCatalog, ItemSchema
from agno_relay.sessions.manager import InMemorySessionStore, SessionManager
from agno_relay.stream.buffer import ThrottledStreamBuffer
from agno_relay.stream.collector import MessageCollector

# --- Session Tests ---


def test_conversation_key_session_topology():
    # 1. Direct message: whole conversation is one key
    dm = ConversationKey(
        platform="teams", chat_id="conv-123", is_direct_message=True, sender_id="user-a"
    )
    assert dm.session_key == "teams:conv-123"

    # 2. Channel thread: per-person per-thread
    thread = ConversationKey(
        platform="teams",
        chat_id="chan-general",
        thread_id="root-msg-99",
        sender_id="user-a",
        is_direct_message=False,
    )
    assert thread.session_key == "teams:chan-general:root-msg-99:user-a"

    # 3. Group chat without thread: per-person in group chat
    group = ConversationKey(
        platform="lark",
        chat_id="oc_group_888",
        sender_id="ou_user_1",
        is_direct_message=False,
    )
    assert group.session_key == "lark:oc_group_888:ou_user_1"


@pytest.mark.asyncio
async def test_session_manager_lifecycle_and_25h_ttl():
    store = InMemorySessionStore()
    manager = SessionManager(store=store, idle_ttl=timedelta(hours=25))

    key = ConversationKey(platform="teams", chat_id="dm-1", is_direct_message=True)

    # 1. First interaction creates new session
    session_id_1, is_new_1 = await manager.get_or_create_session(key)
    assert is_new_1 is True
    assert session_id_1 is not None

    # 2. Subsequent interaction within 25 hours reuses session
    session_id_2, is_new_2 = await manager.get_or_create_session(key)
    assert is_new_2 is False
    assert session_id_2 == session_id_1

    # 3. Simulate 26 hours passing (exceeding 25h idle TTL)
    record = await store.get(key.session_key)
    assert record is not None
    record.last_active_at = datetime.now(UTC) - timedelta(hours=26)
    await store.save(record)

    # 4. Next interaction detects expiration and auto-rotates
    session_id_3, is_new_3 = await manager.get_or_create_session(key)
    assert is_new_3 is True
    assert session_id_3 != session_id_1

    # 5. Explicit close (/reset command)
    await manager.close_session(key, reason="user_command_reset")
    session_id_4, is_new_4 = await manager.get_or_create_session(key)
    assert is_new_4 is True
    assert session_id_4 != session_id_3


# --- Stream & Collector Tests ---


class DummyMovieItem(ItemSchema):
    schema_name = "test-movie"
    id: int
    title: str

    def render_teams(self, resolved):
        return {"type": "TextBlock", "text": f"Teams: {self.title}"}

    def render_lark(self, resolved):
        return {"tag": "div", "text": {"tag": "lark_md", "content": f"**Lark: {self.title}**"}}


class DummyMovieListBlock(BlockSchema):
    schema_name = "test-movie-list"
    body = "items"
    item = DummyMovieItem


@pytest.mark.asyncio
async def test_message_collector_aggregates_items_into_single_card():
    catalog = CardCatalog([DummyMovieListBlock])
    collector = MessageCollector(platform="teams", catalog=catalog)

    # Simulate AG-UI events during agent execution
    collector.feed(
        TextMessageContentEvent(message_id="m1", delta="Here are your recommendations:\n\n")
    )
    collector.feed(
        TextMessageContentEvent(
            message_id="m1",
            delta='```stream-ui {"schema": "test-movie-list", "title": "Top Sci-Fi"}\n',
        )
    )
    collector.feed(
        CustomEvent(
            name="ui.block.start",
            value={"name": "test-movie-list", "props": {"title": "Top Sci-Fi"}},
        )
    )
    collector.feed(
        CustomEvent(
            name="ui.item", value={"name": "test-movie", "data": {"id": 1, "title": "Inception"}}
        )
    )
    collector.feed(
        CustomEvent(
            name="ui.item", value={"name": "test-movie", "data": {"id": 2, "title": "Interstellar"}}
        )
    )
    collector.feed(
        TextMessageContentEvent(
            message_id="m1",
            delta='{"id": 1, "title": "Inception"}\n{"id": 2, "title": "Interstellar"}\n```\n',
        )
    )
    collector.feed(CustomEvent(name="ui.block.end", value={"name": "test-movie-list"}))
    collector.feed(TextMessageContentEvent(message_id="m1", delta="Hope you enjoy them!"))

    outbound = collector.finalize()

    # The stream-ui fence is cleanly stripped from user text
    assert "Here are your recommendations:" in outbound.text
    assert "Hope you enjoy them!" in outbound.text
    assert "```stream-ui" not in outbound.text

    # Exactly ONE aggregated Adaptive Card containing both items
    assert len(outbound.cards) == 1
    card = outbound.cards[0]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["text"] == "Top Sci-Fi"
    assert card["body"][1] == {"type": "TextBlock", "text": "Teams: Inception"}
    assert card["body"][2] == {"type": "TextBlock", "text": "Teams: Interstellar"}


@pytest.mark.asyncio
async def test_throttled_stream_buffer():
    flushed_history = []

    async def on_flush(content: str):
        flushed_history.append(content)

    buffer = ThrottledStreamBuffer(flush_callback=on_flush, min_interval_seconds=0.05)

    await buffer.append("Hello ")
    # First chunk flushes immediately if interval check passes (initial flush)
    assert len(flushed_history) >= 1

    # Appending quickly within 50ms does not flush immediately
    await buffer.append("world! ")
    await buffer.append("How are you?")

    # Wait for interval to pass
    await asyncio.sleep(0.06)
    await buffer.append(" More text.")
    assert len(flushed_history) >= 2

    # Final explicit flush
    await buffer.flush()
    assert flushed_history[-1] == "Hello world! How are you? More text."
