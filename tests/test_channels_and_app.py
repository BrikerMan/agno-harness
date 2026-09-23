import pytest
from rich.console import Console

from agno_harness import (
    AgentRuntime,
    ChannelEvent,
    CLIChannel,
    ConversationKey,
    InMemorySink,
    LarkChannel,
    RelayApp,
    SQLiteSink,
    StreamMode,
    TeamsChannel,
    WebChannel,
)
from agno_harness.core.streamui.schema import BlockSchema, CardCatalog, ItemSchema

from .conftest import FakeAgent, content, run_completed, tool_completed, tool_started


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
    runtime = AgentRuntime(agent=fake_agent, catalog=catalog)

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
    runtime = AgentRuntime(agent=fake_agent)
    app = RelayApp(runtime)
    cli_channel = CLIChannel(console=Console(record=True))
    app.add_channel(cli_channel)

    event = ChannelEvent(
        event_id="reset-1",
        key=ConversationKey(platform="cli", chat_id="chat-reset", is_direct_message=True),
        text="/reset",
    )

    outbound = await app.handle_event(cli_channel, event)
    assert "cleared" in outbound.text.lower()


@pytest.mark.asyncio
async def test_relay_start_leaves_cli_channel_running():
    runtime = AgentRuntime(agent=FakeAgent([]))
    app = RelayApp(runtime)
    cli = CLIChannel(console=Console(record=True))
    app.add_channel(cli)

    await app.start()
    try:
        assert cli._running is True
    finally:
        await app.stop()
    assert cli._running is False


@pytest.mark.asyncio
async def test_cli_interactive_loop_runs_until_exit(monkeypatch):
    runtime = AgentRuntime(agent=FakeAgent([]))
    app = RelayApp(runtime)
    cli = CLIChannel(console=Console(record=True))
    app.add_channel(cli)

    await app.start()
    try:
        monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: "/exit")
        await cli.run_interactive_loop()
    finally:
        await app.stop()

    output = cli.console.export_text()
    assert "CLI ready" in output
    assert "Bye." in output


@pytest.mark.asyncio
async def test_cli_raw_mode_streams_text_deltas():
    runtime = AgentRuntime(
        agent=FakeAgent([content("Hel"), content("lo "), content("world"), run_completed()])
    )
    app = RelayApp(runtime)
    cli = CLIChannel(console=Console(record=True))
    app.add_channel(cli)
    assert app.channels["cli"][1] is StreamMode.RAW

    deltas: list[str] = []
    original = cli.stream_chunk

    async def capture(destination, message_id, delta):
        deltas.append(delta)
        await original(destination, message_id, delta)

    cli.stream_chunk = capture  # type: ignore[method-assign]

    outbound = await app.handle_event(
        cli,
        ChannelEvent(
            event_id="stream-1",
            key=ConversationKey(platform="cli", chat_id="c1", is_direct_message=True),
            text="hi",
        ),
    )

    assert outbound.text == "Hello world"
    assert len(deltas) > 1
    assert "".join(deltas) == "Hello world"
    assert "Hello world" in cli.console.export_text()


@pytest.mark.asyncio
async def test_cli_final_mode_does_not_stream_chunks():
    runtime = AgentRuntime(agent=FakeAgent([content("Hello world"), run_completed()]))
    app = RelayApp(runtime)
    cli = CLIChannel(console=Console(record=True))
    app.add_channel(cli, stream_mode=StreamMode.FINAL)

    deltas: list[str] = []

    async def capture(destination, message_id, delta):
        deltas.append(delta)

    cli.stream_chunk = capture  # type: ignore[method-assign]

    outbound = await app.handle_event(
        cli,
        ChannelEvent(
            event_id="final-1",
            key=ConversationKey(platform="cli", chat_id="c1", is_direct_message=True),
            text="hi",
        ),
    )

    assert outbound.text == "Hello world"
    assert deltas == []
    assert "Hello world" in cli.console.export_text()


@pytest.mark.asyncio
async def test_cli_tool_panel_includes_params():
    runtime = AgentRuntime(
        agent=FakeAgent(
            [
                tool_started("c1", "duckduckgo_search", {"query": "Python 3.13"}),
                tool_completed("c1", "duckduckgo_search", {"ok": True}),
                content("Here is what I found."),
                run_completed(),
            ]
        )
    )
    app = RelayApp(runtime)
    cli = CLIChannel(console=Console(record=True))
    app.add_channel(cli)

    await app.handle_event(
        cli,
        ChannelEvent(
            event_id="tool-1",
            key=ConversationKey(platform="cli", chat_id="c1", is_direct_message=True),
            text="search python",
        ),
    )

    output = cli.console.export_text()
    assert "duckduckgo_search" in output
    assert "Python 3.13" in output
    assert "Here is what I found." in output


@pytest.mark.asyncio
async def test_cli_shows_reasoning_content():
    runtime = AgentRuntime(
        agent=FakeAgent(
            [
                content("", reasoning="Let me think about this."),
                content("Here is the answer."),
                run_completed(),
            ]
        )
    )
    app = RelayApp(runtime)
    cli = CLIChannel(console=Console(record=True))
    app.add_channel(cli)

    outbound = await app.handle_event(
        cli,
        ChannelEvent(
            event_id="reason-1",
            key=ConversationKey(platform="cli", chat_id="c1", is_direct_message=True),
            text="hi",
        ),
    )

    output = cli.console.export_text()
    assert outbound.text == "Here is the answer."
    assert "Let me think about this." in output
    assert "Here is the answer." in output


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
    assert "/api/v1/channels/teams/messages" in routes

    # 4. WebChannel
    runtime = AgentRuntime(agent=FakeAgent([]))
    web = WebChannel(runtime=runtime)
    assert web.name == "web"
    web_router = web.get_router()
    web_routes = [r.path for r in web_router.routes]
    assert "/api/v1/channels/web/agui" in web_routes


def test_message_collector_renders_hitl_cards():
    from ag_ui.core import CustomEvent, EventType

    from agno_harness.stream.collector import MessageCollector

    # 1. Teams collector
    teams_collector = MessageCollector(platform="teams")
    pause_event = CustomEvent(
        type=EventType.CUSTOM,
        name="run.paused",
        value={
            "pauseType": "confirmation",
            "toolCallId": "call_123",
            "toolName": "delete_database",
            "toolArgs": {"db_name": "prod"},
        },
    )
    teams_collector.feed(pause_event)
    teams_outbound = teams_collector.finalize()

    assert len(teams_outbound.cards) == 1
    card = teams_outbound.cards[0]
    assert card["type"] == "AdaptiveCard"
    action_data = card["actions"][0]["data"]
    assert action_data["action_id"] == "agno.hitl.resume"
    assert action_data["tool_call_id"] == "call_123"
    assert action_data["accepted"] is True

    # 2. Lark collector
    lark_collector = MessageCollector(platform="lark")
    lark_collector.feed(pause_event)
    lark_outbound = lark_collector.finalize()

    assert len(lark_outbound.cards) == 1
    lark_card = lark_outbound.cards[0]
    actions = lark_card["elements"][1]["actions"]
    assert actions[0]["value"]["action_id"] == "agno.hitl.resume"
    assert actions[0]["value"]["tool_call_id"] == "call_123"
    assert actions[0]["value"]["accepted"] is True
