"""SubAgentToolkit — one tool for many registered sub-agents."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agno.agent import Agent

from agno_harness import SubAgentToolkit
from agno_harness.tools.subagent import (
    TOOL_NAME,
    build_continuation_prompt,
    clear_partial_state,
    load_partial_state,
    safe_cut,
    save_partial_state,
)

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
        from agno_harness import AgentRuntime
        from agno_harness.stores import Stores

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

        runtime = AgentRuntime(agent=FakeAgent([]), db=None, stores=Stores())
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

    async def test_subagent_retries_on_first_token_timeout_and_succeeds(self):
        import asyncio

        attempts = 0

        class HangingThenFastAgent:
            name = "reviewer"
            description = "Reviews code."

            async def arun(self, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    # Hang longer than first_chunk_timeout
                    await asyncio.sleep(0.2)
                    yield content("late chunk")
                else:
                    yield content("fast chunk")
                    yield run_completed()

        agent = HangingThenFastAgent()
        toolkit = SubAgentToolkit(
            agents=[agent],  # type: ignore[arg-type]
            first_chunk_timeout=0.05,
            inter_chunk_timeout=0.1,
            max_attempts=2,
        )
        result = await toolkit.delegate_subagent(
            agent_name="reviewer",
            description="testing retry",
            prompt="do it",
        )
        assert attempts == 2
        assert "fast chunk" in result

    async def test_subagent_exhausts_retries_and_raises_timeout_error(self):
        import asyncio

        attempts = 0

        class AlwaysHangingAgent:
            name = "reviewer"
            description = "Reviews code."

            async def arun(self, **kwargs):
                nonlocal attempts
                attempts += 1
                await asyncio.sleep(0.5)
                yield content("never reached")

        agent = AlwaysHangingAgent()
        toolkit = SubAgentToolkit(
            agents=[agent],  # type: ignore[arg-type]
            first_chunk_timeout=0.05,
            inter_chunk_timeout=0.05,
            max_attempts=2,
        )
        with pytest.raises(TimeoutError, match="upstream model stream timed out"):
            await toolkit.delegate_subagent(
                agent_name="reviewer",
                description="testing exhaustion",
                prompt="do it",
            )
        assert attempts == 2

    async def test_subagent_seals_interrupted_run_and_preserves_original_prompt_on_resume(self):
        import asyncio
        import re

        from agno.db.in_memory import InMemoryDb
        from agno.models.message import Message
        from agno.run.agent import RunOutput
        from agno.run.base import RunStatus
        from agno.session.agent import AgentSession

        db = InMemoryDb()
        received_inputs: list[str] = []
        received_history_flags: list[bool] = []
        call_count = 0

        class StallingThenResumingAgent:
            name = "ip-recommendation-agent"
            description = "Recommends IPs."

            def __init__(self):
                self.db = db

            async def arun(self, **kwargs):
                nonlocal call_count
                call_count += 1
                sid = kwargs["session_id"]
                inp = kwargs["input"]
                received_inputs.append(inp)
                received_history_flags.append(bool(kwargs.get("add_history_to_context")))

                if call_count == 1:
                    # Simulate Agno persisting a cancelled run with 18 IPs and tool results
                    run_out = RunOutput(
                        run_id="run-1",
                        agent_id=self.name,
                        session_id=sid,
                        status=RunStatus.cancelled,
                        messages=[
                            Message(role="user", content=inp),
                            Message(
                                role="assistant",
                                content="Fetching 18 IPs...",
                                tool_calls=[
                                    {
                                        "id": "call_ips",
                                        "type": "function",
                                        "function": {"name": "batch_search_ips", "arguments": "{}"},
                                    }
                                ],
                            ),
                            Message(
                                role="tool",
                                tool_call_id="call_ips",
                                tool_name="batch_search_ips",
                                content="[{'name': 'TMNT'}, {'name': 'Godzilla x Kong'}]",
                            ),
                        ],
                    )
                    db.upsert_session(AgentSession(session_id=sid, runs=[run_out]))
                    yield content("Now let me fetch market signals for all 18 IPs.\n\n")
                    await asyncio.sleep(0.2)
                else:
                    # On resume, verify sealed history from run-1 is accessible via get_messages()
                    sess = db.get_session(session_id=sid)
                    assert sess is not None
                    hist = sess.get_messages()
                    assert any("TMNT" in str(m.content) for m in hist)
                    yield content("Completed 18 IPs report for TMNT and Godzilla x Kong.")
                    yield run_completed()

        agent = StallingThenResumingAgent()
        toolkit = SubAgentToolkit(
            agents=[agent],  # type: ignore[arg-type]
            first_chunk_timeout=0.05,
            inter_chunk_timeout=0.05,
            max_attempts=1,
        )

        original_18_ips_prompt = (
            "Evaluate these 18 specific IPs: 1. Teenage Mutant Ninja Turtles, "
            "2. Godzilla x Kong, 3. Kung Fu Panda, 4. Vikings: Valhalla"
        )
        session_id = "ip-recommendation-agent-test18ips"

        # Turn 1: times out after streaming partial text
        with pytest.raises(TimeoutError):
            await toolkit.delegate_subagent(
                agent_name="ip-recommendation-agent",
                description="Rank 18 IPs",
                prompt=original_18_ips_prompt,
                session_id=session_id,
            )

        # Turn 2: parent orchestrator resumes with a short "continue" prompt omitting the 18 IP names
        resumed_result = await toolkit.delegate_subagent(
            agent_name="ip-recommendation-agent",
            description="Continue 18 IPs",
            prompt="继续上一轮未完成的任务：基于此前已确认的 18 个 IP 实体输出排名榜单",
            session_id=session_id,
        )

        assert len(received_inputs) == 2
        # Turn 2 input MUST still contain the Turn 1 original 18 IPs prompt as ORIGINAL TASK!
        assert "Teenage Mutant Ninja Turtles" in received_inputs[1]
        assert "Godzilla x Kong" in received_inputs[1]
        assert "继续上一轮未完成的任务" in received_inputs[1]
        assert "Now let me fetch market signals for all 18 IPs." in received_inputs[1]
        assert received_history_flags[1] is True
        assert "Completed 18 IPs report for TMNT and Godzilla x Kong." in resumed_result
        assert re.search(r"\[subagent_session:\s*ip-recommendation-agent-test18ips", resumed_result)

    def test_partial_state_roundtrip_and_safe_cut(self, tmp_path):
        partial_file = tmp_path / ".partial-sub-1.md"
        orig = "Analyze 18 IPs: TMNT, Kung Fu Panda, Cyberpunk 2077"
        raw_written = (
            "Intro paragraph.\n\n"
            "<stream-ui>\n"
            '{"schema": "artifact", "title": "Half Report", "path": "research/r.md"}\n'
            "# Unclosed body"
        )
        cut = safe_cut(raw_written)
        assert cut == "Intro paragraph.\n\n"

        save_partial_state(partial_file, original_prompt=orig, written=cut)
        loaded_orig, loaded_written = load_partial_state(partial_file)
        assert loaded_orig == orig
        assert loaded_written == "Intro paragraph.\n\n"

        cont_prompt = build_continuation_prompt(
            loaded_orig,
            loaded_written,
            dropped_block=True,
            resume_instruction="Continue from where you stopped",
        )
        assert orig in cont_prompt
        assert "Continue from where you stopped" in cont_prompt
        assert "Intro paragraph." in cont_prompt
        assert "Never substitute, guess, or fetch unrelated top-ranking items" in cont_prompt

        clear_partial_state(partial_file)
        assert not partial_file.exists()
