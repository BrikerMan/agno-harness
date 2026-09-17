import pytest
from rich.console import Console

from agno_relay import (
    AguiRuntime,
    ChannelEvent,
    CLIChannel,
    ConversationKey,
    InMemorySink,
    LarkChannel,
    RelayApp,
    SQLiteSink,
    TeamsChannel,
    WebChannel,
)
from agno_relay.core.streamui.schema import BlockSchema, CardCatalog, ItemSchema

from .conftest import FakeAgent, content, run_completed


class ItemCard(ItemSchema):
    schema_name = "test-item"
    id: int
    label: str

    def render_teams(self, resolved):
        return {"type": "TextBlock", "text": f"Teams Item: {self.label}"}

    def render_lark(self, resolved):
        return {"tag": "div", "text": {"tag": "lark_md", "content": f"**Lark Item: {self.label}**"}}


class ListBlock(BlockSchema):
    schema_name = "test-list"
    body = "items"
    item = ItemCard


@pytest.mark.asyncio
async def test_relay_app_end_to_end_routing_and_sinks(tmp_path):
    catalog = CardCatalog([ListBlock])
    db_file = str(tmp_path / "test_audit.db")
    sqlite_sink = SQLiteSink(db_path=db_file)
    memory_sink = InMemorySink()

    # Create agent that yields text and a card block
    chunks = [
        content(
            "Hello! Here is your card:\n"
            '```stream-ui {"schema": "test-list", "title": "My Items"}\n'
            '{"id": 1, "label": "First"}\n'
            '{"id": 2, "label": "Second"}\n'
            "```\n"
            "Enjoy!"
        ),
        run_completed(),
    ]
    fake_agent = FakeAgent(chunks)
    runtime = AguiRuntime(agent=fake_agent, catalog=catalog)

    app = RelayApp(runtime, card_catalog=catalog)
    app.add_sink(sqlite_sink)
    app.add_sink(memory_sink)

    cli_channel = CLIChannel(console=Console(record=True))
    app.add_channel(cli_channel)

    inbound_event = ChannelEvent(
        event_id="evt-100",
        key=ConversationKey(platform="cli", chat_id="chat-1", is_direct_message=True),
        text="Show me items",
    )

    outbound = await app.handle_event(cli_channel, inbound_event)

    # 1. Output text was stripped cleanly of stream-ui XML/fences
    assert "Hello! Here is your card:" in outbound.text
    assert "Enjoy!" in outbound.text
    assert "```stream-ui" not in outbound.text

    # 2. Aggregated cards generated
    assert len(outbound.cards) == 1
    card = outbound.cards[0]
    # CLI fallback block contains title and items
    assert "[ My Items ]" in card

    # 3. Memory sink recorded both inbound and outbound
    assert len(memory_sink.records) == 2
    assert memory_sink.records[0].direction == "inbound"
    assert memory_sink.records[0].text == "Show me items"
    assert memory_sink.records[1].direction == "outbound"
    assert "Hello!" in memory_sink.records[1].text
    assert len(memory_sink.records[1].cards) == 1

    # 4. SQLite sink recorded into database
    history = await sqlite_sink.get_history("chat-1")
    assert len(history) == 2
    assert history[0]["direction"] == "inbound"
    assert history[0]["text"] == "Show me items"
    assert history[1]["direction"] == "outbound"
    assert "Hello!" in history[1]["text"]


@pytest.mark.asyncio
async def test_relay_app_session_reset_command():
    fake_agent = FakeAgent([content("Should not be called"), run_completed()])
    app = RelayApp(fake_agent)
    cli_channel = CLIChannel(console=Console(record=True))
    app.add_channel(cli_channel)

    event = ChannelEvent(
        event_id="reset-1",
        key=ConversationKey(platform="cli", chat_id="chat-reset", is_direct_message=True),
        text="/reset",
    )

    outbound = await app.handle_event(cli_channel, event)
    assert "cleared" in outbound.text.lower()


def test_channel_instantiations_and_routers():
    # 1. CLIChannel
    cli = CLIChannel()
    assert cli.name == "cli"

    # 2. LarkChannel
    lark = LarkChannel(app_id="cli_123", app_secret="sec_456")
    assert lark.name == "lark"

    # 3. TeamsChannel
    teams = TeamsChannel(bot_app_id="app_789")
    assert teams.name == "teams"
    router = teams.get_router()
    routes = [r.path for r in router.routes]
    assert "/api/messages" in routes

    # 4. WebChannel
    runtime = AguiRuntime(agent=FakeAgent([]))
    web = WebChannel(runtime=runtime)
    assert web.name == "web"
    web_router = web.get_router()
    web_routes = [r.path for r in web_router.routes]
    assert "/agui" in web_routes
