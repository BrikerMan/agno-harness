"""SubAgentToolkit — one tool for many registered sub-agents."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agno.agent import Agent

from agno_relay import SubAgentToolkit
from agno_relay.tools.subagent import TOOL_NAME

from .conformance import assert_valid_agui_sequence
from .conftest import (
    FakeAgent,
    content,
    customs,
    make_input,
    run_completed,
    text_of,
    tool_completed,
    tool_started,
)


def _named(name: str, description: str = "") -> Agent:
    return Agent(name=name, description=description or None, telemetry=False)


class TestSubAgentToolkitConstruction:
    def test_requires_at_least_one_agent(self):
        with pytest.raises(ValueError, match="at least one"):
            SubAgentToolkit(agents=[])

    def test_names_must_be_unique(self):
        with pytest.raises(ValueError, match="unique"):
            SubAgentToolkit(agents=[_named("a"), _named("a")])

    def test_roster_is_injected_via_add_instructions(self):
        toolkit = SubAgentToolkit(
            agents=[
                _named("reviewer", "Reviews code."),
                _named("researcher", "Searches the web."),
            ]
        )
        assert toolkit.add_instructions is True
        assert "delegate_subagent" in (toolkit.instructions or "")
        assert "reviewer — Reviews code." in (toolkit.instructions or "")
        assert "researcher — Searches the web." in (toolkit.instructions or "")
        assert toolkit.tool_names == [TOOL_NAME]


class TestSubAgentToolkitDelegation:
    async def test_streams_through_substream_by_default(self):
        from agno_relay import AguiRuntime
        from agno_relay.stores import Stores

        reviewer = FakeAgent([content("The sign is wrong."), run_completed()])
        reviewer.name = "reviewer"
        reviewer.description = "Reviews code."
        toolkit = SubAgentToolkit(agents=[reviewer])  # type: ignore[arg-type]
        held: dict[str, str] = {}

        async def parent(**kwargs):
            yield tool_started(
                "t1",
                TOOL_NAME,
                {
                    "agent_name": "reviewer",
                    "description": "Reviewing add()",
                    "prompt": "def add(a, b): return a - b",
                },
            )
            held["result"] = await toolkit.delegate_subagent(
                agent_name="reviewer",
                description="Reviewing add()",
                prompt="def add(a, b): return a - b",
            )
            yield tool_completed("t1", TOOL_NAME, held["result"])
            yield content("Done.")
            yield run_completed()

        runtime = AguiRuntime(agent=FakeAgent([]), db=None, stores=Stores())
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = [event async for event in runtime.stream_events(make_input())]
        assert_valid_agui_sequence(events)

        starts = customs(events, "subagent.start")
        assert len(starts) == 1
        assert starts[0].value["name"] == "reviewer"
        assert starts[0].value["description"] == "Reviewing add()"
        assert customs(events, "subagent.end")
        assert "The sign is wrong." in text_of(events)
        assert "subagent_session:" in held["result"]

    async def test_unknown_agent_raises(self):
        toolkit = SubAgentToolkit(agents=[_named("reviewer")])
        with pytest.raises(ValueError, match="Unknown sub-agent"):
            await toolkit.delegate_subagent(
                agent_name="ghost",
                description="nope",
                prompt="x",
            )

    async def test_blocking_mode_skips_substream(self):
        class BlockingAgent:
            name = "reviewer"
            description = "Reviews code."

            async def arun(self, **kwargs):
                assert kwargs.get("stream") is False
                return SimpleNamespace(content="ok")

        toolkit = SubAgentToolkit(agents=[BlockingAgent()], stream=False)  # type: ignore[arg-type]
        result = await toolkit.delegate_subagent(
            agent_name="reviewer",
            description="hi",
            prompt="review this",
        )
        assert result.startswith("ok")
        assert "subagent_session:" in result
