"""Tests for TodoToolkit and todo planning tools."""

from __future__ import annotations

from agno_harness import (
    AgentRuntime,
    BlockSchema,
    CardCatalog,
    ItemSchema,
    TodoToolkit,
    format_todo_list,
    parse_todo_list,
)

from .conformance import assert_valid_agui_sequence
from .conftest import FakeAgent, content, customs, make_input, run_completed


class MockTodoItem(ItemSchema):
    schema_name = "todo"
    label: str
    state: str = "pending"
    detail: str | None = None
    level: int = 0


class MockTodoList(BlockSchema):
    schema_name = "todo-list"
    item = MockTodoItem
    title: str | None = None


def _catalog() -> CardCatalog:
    cat = CardCatalog()
    cat.register(MockTodoList)
    return cat


class TestTodoToolkitParsing:
    def test_parses_two_level_hierarchy(self):
        markdown = (
            "- [ ] 1. Architecture review\n"
            "- [-] 2. Execution phase\n"
            "  - [x] 2.1 Prepare tools\n"
            "  - [-] 2.2 Run batch computation\n"
            "- [ ] 3. Final summary\n"
        )
        items = parse_todo_list(markdown)
        assert items == [
            {"label": "1. Architecture review", "state": "pending"},
            {"label": "2. Execution phase", "state": "doing"},
            {"label": "2.1 Prepare tools", "state": "done", "level": 1},
            {"label": "2.2 Run batch computation", "state": "doing", "level": 1},
            {"label": "3. Final summary", "state": "pending"},
        ]

    def test_parses_flat_list_when_subtasks_disabled(self):
        markdown = (
            "- [ ] 1. Architecture review\n"
            "- [-] 2. Execution phase\n"
            "  - [x] 2.1 Prepare tools\n"
            "  - [-] 2.2 Run batch computation\n"
            "- [ ] 3. Final summary\n"
        )
        items = parse_todo_list(markdown, allow_subtasks=False)
        assert items == [
            {"label": "1. Architecture review", "state": "pending"},
            {"label": "2. Execution phase", "state": "doing"},
            {"label": "2.1 Prepare tools", "state": "done"},
            {"label": "2.2 Run batch computation", "state": "doing"},
            {"label": "3. Final summary", "state": "pending"},
        ]

    def test_formats_items_back_to_markdown(self):
        items = [
            {"label": "Main step", "state": "doing"},
            {"label": "Sub step", "state": "done", "level": 1},
        ]
        text = format_todo_list(items)
        assert text == "- [-] Main step\n  - [x] Sub step"


class TestTodoToolkitExecution:
    def test_instructions_defaults_to_flat_and_supports_subtasks_flag(self):
        # Class call defaults to flat
        default_inst = TodoToolkit.instructions()
        assert "todo_write" in default_inst
        assert "without indented sub-tasks" in default_inst
        assert "sub-tasks:" not in default_inst

        # Class call with allow_subtasks=True
        subtask_inst = TodoToolkit.instructions(allow_subtasks=True)
        assert "Group complex steps with 2-space indented sub-tasks" in subtask_inst
        assert "CRITICAL RULE" in subtask_inst

        # Instance call inherits allow_subtasks configuration
        t_flat = TodoToolkit()
        assert t_flat.allow_subtasks is False
        assert "without indented sub-tasks" in t_flat.instructions()

        t_sub = TodoToolkit(allow_subtasks=True)
        assert t_sub.allow_subtasks is True
        assert "Group complex steps with 2-space indented sub-tasks" in t_sub.instructions()

    async def test_toolkit_emits_flat_list_by_default(self):
        toolkit = TodoToolkit(default_title="Flat Plan")
        assert toolkit.allow_subtasks is False

        async def parent(**kwargs):
            await toolkit.todo_write(
                todos="- [x] Setup\n- [-] Build\n  - [x] Sub-task\n- [ ] Deploy",
                title="Flat CI",
            )
            yield content("Pipeline updated.")
            yield run_completed()

        runtime = AgentRuntime(agent=FakeAgent([]), catalog=_catalog())
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = [event async for event in runtime.stream_events(make_input())]
        assert_valid_agui_sequence(events)

        items = customs(events, "ui.item")
        assert len(items) == 4
        # Since allow_subtasks is False by default, all items remain top-level (level == 0)
        assert all(i.value["data"].get("level", 0) == 0 for i in items)

    async def test_toolkit_emits_todo_list_block_with_subtasks_when_enabled(self):
        toolkit = TodoToolkit(default_title="Build Pipeline", allow_subtasks=True)
        assert toolkit.allow_subtasks is True

        async def parent(**kwargs):
            summary = await toolkit.todo_write(
                todos="- [x] Setup\n- [-] Build\n  - [x] Compile\n  - [-] Test\n- [ ] Deploy",
                title="CI Pipeline",
            )
            assert "60% complete" in summary or "complete" in summary
            yield content("Pipeline updated.")
            yield run_completed()

        runtime = AgentRuntime(
            agent=FakeAgent([]),
            catalog=_catalog(),
        )
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = [event async for event in runtime.stream_events(make_input())]
        assert_valid_agui_sequence(events)

        block_starts = customs(events, "ui.block.start")
        assert len(block_starts) == 1
        assert block_starts[0].value["schema"] == "todo-list"
        assert block_starts[0].value["props"]["title"] == "CI Pipeline"

        items = customs(events, "ui.item")
        assert len(items) == 5
        assert [i.value["data"]["state"] for i in items] == [
            "done",
            "doing",
            "done",
            "doing",
            "pending",
        ]
        assert items[2].value["data"].get("level") == 1
        assert items[3].value["data"].get("level") == 1

    def test_agno_agent_auto_injects_instructions(self):
        from agno.agent import Agent
        from agno.agent._tools import parse_tools
        from agno.models.openai import OpenAIChat

        agent_flat = Agent(
            model=OpenAIChat(id="gpt-4o"),
            tools=[TodoToolkit(allow_subtasks=False)],
        )
        parse_tools(agent_flat, agent_flat.tools, agent_flat.model, async_mode=True)
        assert agent_flat._tool_instructions is not None
        assert len(agent_flat._tool_instructions) == 1
        assert "without indented sub-tasks" in agent_flat._tool_instructions[0]

        agent_sub = Agent(
            model=OpenAIChat(id="gpt-4o"),
            tools=[TodoToolkit(allow_subtasks=True)],
        )
        parse_tools(agent_sub, agent_sub.tools, agent_sub.model, async_mode=True)
        assert agent_sub._tool_instructions is not None
        assert len(agent_sub._tool_instructions) == 1
        assert (
            "Group complex steps with 2-space indented sub-tasks" in agent_sub._tool_instructions[0]
        )
