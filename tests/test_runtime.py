"""End-to-end runtime behaviour, driven by scripted chunks."""

from __future__ import annotations

import asyncio
import json

import pytest
from ag_ui.core import CustomEvent, EventType

from agno_harness import (
    AgentRuntime,
    HideToolFilter,
    RedactFilter,
    SequencerMode,
    TransformResultFilter,
)
from agno_harness.runtime import (
    EVENT_RUN_PAUSED,
    EVENT_SUBAGENT_END,
    EVENT_SUBAGENT_START,
)
from agno_harness.runtime.parsers import subagent_steps_parser

from .conformance import assert_valid_agui_sequence, messages_from_events
from .conftest import (
    FakeAgent,
    collect,
    content,
    customs,
    make_input,
    run_completed,
    run_paused,
    text_of,
    tool_completed,
    tool_execution,
    tool_started,
    types_of,
)


def runtime_for(chunks, **kwargs):
    kwargs.setdefault("sequencer_mode", SequencerMode.AUDIT)
    return AgentRuntime(agent=FakeAgent(chunks), **kwargs)


async def stream(runtime, run_input=None):
    events = await collect(runtime.stream_events(run_input or make_input()))
    assert_valid_agui_sequence(events)
    return events


class TestPlainStreaming:
    async def test_text_deltas_become_one_message(self):
        events = await stream(runtime_for([content("Hello, "), content("world."), run_completed()]))
        assert types_of(events) == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]
        assert text_of(events) == "Hello, world."

    async def test_a_run_with_no_content_emits_no_empty_message(self):
        events = await stream(runtime_for([run_completed()]))
        assert types_of(events) == ["RUN_STARTED", "RUN_FINISHED"]

    async def test_run_ids_are_carried_through(self):
        events = await stream(runtime_for([run_completed()]))
        assert events[0].thread_id == "thread-1"
        assert events[0].run_id == "run-1"
        assert events[-1].run_id == "run-1"

    async def test_missing_ids_are_generated(self):
        run_input = make_input(thread_id="", run_id="")
        events = await stream(runtime_for([run_completed()]), run_input)
        assert events[0].thread_id
        assert events[0].run_id


class TestErrors:
    async def test_an_exception_becomes_run_error(self):
        runtime = AgentRuntime(
            agent=FakeAgent([content("partial")], raise_at=1),
            sequencer_mode=SequencerMode.AUDIT,
        )
        events = await collect(runtime.stream_events(make_input()))
        assert_valid_agui_sequence(events)
        assert types_of(events)[-1] == "RUN_ERROR"
        assert "model exploded" in events[-1].message
        assert events[-1].raw_event == {"threadId": "thread-1", "runId": "run-1"}

    async def test_an_open_message_is_closed_before_run_error(self):
        runtime = AgentRuntime(
            agent=FakeAgent([content("partial")], raise_at=1),
            sequencer_mode=SequencerMode.AUDIT,
        )
        events = await collect(runtime.stream_events(make_input()))
        assert types_of(events) == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_ERROR",
        ]

    async def test_an_agno_run_error_chunk_becomes_run_error(self):
        # Agno reports a failed run as a chunk and then ends the stream
        # normally; without this the client would be told the run succeeded.
        from agno.run.agent import RunErrorEvent as AgnoRunError

        events = await stream(
            runtime_for([content("Working"), AgnoRunError(content="upstream 503")])
        )
        assert types_of(events)[-1] == "RUN_ERROR"
        assert events[-1].message == "upstream 503"
        assert events[-1].code == "RunError"

    async def test_a_cancelled_run_becomes_run_error(self):
        from agno.run.agent import RunCancelledEvent

        from agno_harness.runtime import EVENT_RUN_CANCELLED

        events = await stream(runtime_for([RunCancelledEvent(reason="user aborted")]))
        cancelled_events = [e for e in events if getattr(e, "name", None) == EVENT_RUN_CANCELLED]
        assert len(cancelled_events) == 1
        assert cancelled_events[0].value.get("reason") == "agent_cancelled"
        assert types_of(events)[-1] == "RUN_ERROR"
        assert events[-1].code == "RunCancelled"
        assert "user aborted" in events[-1].message

    async def test_client_disconnect_stops_quietly(self):
        class Disconnected:
            async def is_disconnected(self):
                return True

        runtime = runtime_for([content("never sent"), run_completed()])
        events = await collect(runtime.stream_events(make_input(), request=Disconnected()))
        assert types_of(events) == ["RUN_STARTED"]


class TestPreRunHooks:
    async def test_hook_events_land_after_run_started(self):
        async def billing(scope):
            yield CustomEvent(
                type=EventType.CUSTOM, name="billing", value={"threadId": scope.thread_id}
            )

        runtime = runtime_for([run_completed()])
        runtime.on_pre_run(billing)
        events = await stream(runtime)
        assert types_of(events) == ["RUN_STARTED", "CUSTOM", "RUN_FINISHED"]
        assert events[1].value == {"threadId": "thread-1"}

    async def test_a_hook_does_not_look_like_a_protocol_violation(self):
        """The runtime opens the run before the hooks, so nothing needs repairing.

        The sequencer would buffer them anyway, but a repair the runtime provokes
        itself would appear in every run's audit trail and bury the real ones.
        """

        async def billing(scope):
            yield CustomEvent(type=EventType.CUSTOM, name="billing", value={})

        runtime = runtime_for([run_completed()])
        runtime.on_pre_run(billing)
        await stream(runtime)
        assert runtime.last_violations() == []

    async def test_hooks_run_in_registration_order(self):
        def make(name):
            async def hook(scope):
                yield CustomEvent(type=EventType.CUSTOM, name=name, value={})

            return hook

        runtime = runtime_for([run_completed()])
        runtime.on_pre_run(make("first")).on_pre_run(make("second"))
        events = await stream(runtime)
        assert [e.name for e in customs(events)] == ["first", "second"]

    async def test_a_hook_can_set_the_user_the_run_is_attributed_to(self):
        """The point of handing hooks the scope: they can change the run."""

        async def entitlement(scope):
            scope.user_id = "resolved-user"
            scope.run_kwargs["retries"] = 2
            if False:  # pragma: no cover - a hook need not yield anything
                yield

        runtime = runtime_for([run_completed()])
        runtime.on_pre_run(entitlement)
        await stream(runtime)
        assert runtime.agent.last_kwargs["user_id"] == "resolved-user"
        assert runtime.agent.last_kwargs["retries"] == 2

    async def test_a_hook_can_turn_on_shared_state_the_client_did_not_ask_for(self):
        async def seed(scope):
            scope.enable_session_state({"plan": "pro"})
            if False:  # pragma: no cover
                yield

        runtime = runtime_for([run_completed()])
        runtime.on_pre_run(seed)
        events = await stream(runtime)
        snapshots = [e for e in events if e.type is EventType.STATE_SNAPSHOT]
        assert snapshots[0].snapshot == {"plan": "pro"}


class TestPostRunHooks:
    async def test_a_post_run_hook_sees_the_terminal_chunk(self):
        seen = {}

        async def summarize(scope, *, completion, error):
            seen["completion"] = type(completion).__name__
            seen["error"] = error
            yield CustomEvent(type=EventType.CUSTOM, name="receipt", value={})

        runtime = runtime_for([content("hi"), run_completed()])
        runtime.on_post_run(summarize)
        events = await stream(runtime)
        assert seen == {"completion": "RunCompletedEvent", "error": None}
        # Inside the run, so the receipt is part of the transcript rather than
        # an orphan frame after the terminal event a client has stopped reading.
        types = types_of(events)
        assert types.index("CUSTOM") < types.index("RUN_FINISHED")
        assert types[-1] == "RUN_FINISHED"

    async def test_a_post_run_hook_sees_the_failure(self):
        from agno.run.agent import RunErrorEvent as AgnoRunError

        async def watcher(scope, *, completion, error):
            yield CustomEvent(
                type=EventType.CUSTOM, name="alert", value={"code": type(error).__name__}
            )

        runtime = runtime_for([AgnoRunError(content="upstream 503")])
        runtime.on_post_run(watcher)
        events = await stream(runtime)
        assert customs(events)[0].value == {"code": "AgentRunFailed"}
        assert types_of(events)[-1] == "RUN_ERROR"


class TestTools:
    async def test_a_tool_call_produces_the_full_lifecycle(self):
        events = await stream(
            runtime_for(
                [
                    tool_started("c1", "get_weather", {"city": "Tokyo"}),
                    tool_completed("c1", "get_weather", {"tempC": 22}),
                    content("It is 22 degrees."),
                    run_completed(),
                ]
            )
        )
        assert types_of(events) == [
            "RUN_STARTED",
            "TOOL_CALL_START",
            "TOOL_CALL_ARGS",
            "TOOL_CALL_END",
            "TOOL_CALL_RESULT",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]

    async def test_hidden_tools_leave_no_trace_at_all(self):
        runtime = runtime_for(
            [
                tool_started("c1", "load_skill", {"name": "internal"}),
                tool_completed("c1", "load_skill", "loaded"),
                content("Done."),
                run_completed(),
            ]
        )
        runtime.register_tool_filter(HideToolFilter(["load_skill"]))
        events = await stream(runtime)
        assert types_of(events) == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]

    async def test_a_visible_tool_survives_alongside_a_hidden_one(self):
        runtime = runtime_for(
            [
                tool_started("c1", "load_skill"),
                tool_completed("c1", "load_skill", "ok"),
                tool_started("c2", "search", {"q": "agno"}),
                tool_completed("c2", "search", "results"),
                run_completed(),
            ]
        )
        runtime.register_tool_filter(HideToolFilter(["load_skill"]))
        events = await stream(runtime)
        starts = [e for e in events if e.type is EventType.TOOL_CALL_START]
        assert [e.tool_call_name for e in starts] == ["search"]

    async def test_results_can_be_transformed(self):
        runtime = runtime_for(
            [
                tool_started("c1", "search"),
                tool_completed("c1", "search", {"huge": "payload"}),
                run_completed(),
            ]
        )
        runtime.register_tool_filter(TransformResultFilter("search", lambda _: "summarized"))
        events = await stream(runtime)
        results = [e for e in events if e.type is EventType.TOOL_CALL_RESULT]
        assert [e.content for e in results] == ["summarized"]

    async def test_secrets_are_redacted_from_args_and_results(self):
        runtime = runtime_for(
            [
                tool_started("c1", "call_api", {"token": "sk-supersecret"}),
                tool_completed("c1", "call_api", "used sk-supersecret"),
                run_completed(),
            ]
        )
        runtime.register_tool_filter(RedactFilter(["sk-supersecret"]))
        events = await stream(runtime)
        blob = json.dumps([e.model_dump(mode="json") for e in events])
        assert "sk-supersecret" not in blob
        assert "[redacted]" in blob

    async def test_parallel_tool_calls_stay_distinct(self):
        events = await stream(
            runtime_for(
                [
                    tool_started("c1", "a"),
                    tool_started("c2", "b"),
                    tool_completed("c1", "a", 1),
                    tool_completed("c2", "b", 2),
                    run_completed(),
                ]
            )
        )
        starts = [e for e in events if e.type is EventType.TOOL_CALL_START]
        assert [e.tool_call_id for e in starts] == ["c1", "c2"]


class TestReasoning:
    async def test_reasoning_content_on_a_run_content_chunk_is_surfaced(self):
        events = await stream(
            runtime_for(
                [content("", reasoning="Let me think."), content("Answer."), run_completed()]
            )
        )
        assert "REASONING_MESSAGE_CONTENT" in types_of(events)
        reasoning = [e for e in events if e.type is EventType.REASONING_MESSAGE_CONTENT]
        assert reasoning[0].delta == "Let me think."
        assert text_of(events) == "Answer."

    async def test_pure_reasoning_does_not_open_an_empty_text_message(self):
        events = await stream(runtime_for([content("", reasoning="thinking"), run_completed()]))
        assert "TEXT_MESSAGE_START" not in types_of(events)

    async def test_the_patch_can_be_disabled(self):
        runtime = runtime_for(
            [content("", reasoning="hidden"), run_completed()],
            enable_reasoning_patch=False,
        )
        events = await stream(runtime)
        assert "REASONING_MESSAGE_CONTENT" not in types_of(events)

    async def test_reasoning_is_closed_before_the_answer_opens(self):
        """A thinking model streams reasoning, then the answer. Agno closes
        neither until the run ends, which strands REASONING_END past the text."""
        events = await stream(
            runtime_for(
                [
                    content("", reasoning="The user said hi."),
                    content("", reasoning=" I should greet them."),
                    content("Hi"),
                    content("! How can I help?"),
                    run_completed(),
                ]
            )
        )
        assert types_of(events) == [
            "RUN_STARTED",
            "REASONING_START",
            "REASONING_MESSAGE_START",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_END",
            "REASONING_END",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]

    async def test_reasoning_either_side_of_a_tool_call_stays_separate(self):
        events = await stream(
            runtime_for(
                [
                    content("", reasoning="I need to search."),
                    tool_started("c1", "search", {"q": "hi"}),
                    tool_completed("c1", "search", "found it"),
                    content("", reasoning="Now I know."),
                    content("Here you go."),
                    run_completed(),
                ]
            )
        )
        assert types_of(events) == [
            "RUN_STARTED",
            "REASONING_START",
            "REASONING_MESSAGE_START",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_END",
            "REASONING_END",
            "TOOL_CALL_START",
            "TOOL_CALL_ARGS",
            "TOOL_CALL_END",
            "TOOL_CALL_RESULT",
            "REASONING_START",
            "REASONING_MESSAGE_START",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_END",
            "REASONING_END",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]

    async def test_a_stray_newline_of_reasoning_does_not_cut_the_answer_in_two(self):
        """Qwen sprinkles a lone ``"\\n"`` of reasoning between content chunks.

        Acting on it costs an answer: Agno closes the open text message before
        starting a reasoning session, and the rest of the answer arrives as a
        second message. Anything spanning the seam — a StreamUI fence above all
        — is broken by it, and the thinking block it buys is empty.
        """
        events = await stream(
            runtime_for(
                [
                    content("Here is "),
                    content("", reasoning="\n"),
                    content("the answer."),
                    run_completed(),
                ]
            )
        )
        assert "REASONING_START" not in types_of(events)
        assert types_of(events).count("TEXT_MESSAGE_START") == 1
        assert text_of(events) == "Here is the answer."

    async def test_a_newline_inside_thinking_is_kept(self):
        """The blank only goes when it would open a session; paragraph breaks in
        the middle of a thought are how the thought is meant to read."""
        events = await stream(
            runtime_for(
                [
                    content("", reasoning="First."),
                    content("", reasoning="\n\n"),
                    content("", reasoning="Second."),
                    content("Done."),
                    run_completed(),
                ]
            )
        )
        deltas = [e.delta for e in events if e.type is EventType.REASONING_MESSAGE_CONTENT]
        assert "".join(deltas) == "First.\n\nSecond."

    async def test_the_two_sessions_do_not_share_a_message_id(self):
        """Agno keeps one reasoning id per run, so both sessions arrive as the
        same id and a client keying blocks by it would merge them."""
        runtime = runtime_for(
            [
                content("", reasoning="I need to search."),
                tool_started("c1", "search", {"q": "hi"}),
                tool_completed("c1", "search", "found it"),
                content("", reasoning="Now I know."),
                content("Here you go."),
                run_completed(),
            ]
        )
        events = await stream(runtime)
        ids = [e.message_id for e in events if e.type is EventType.REASONING_MESSAGE_CONTENT]
        assert len(ids) == 2
        assert ids[0] != ids[1]
        assert "reasoning_id_reused" in [v.rule for v in runtime.last_violations()]


class TestSharedState:
    async def test_client_state_is_bracketed_by_snapshots(self):
        # Opening snapshot so the client knows the agent's starting view, and a
        # closing one so it can reconcile after the deltas.
        run_input = make_input(state={"cart": []})
        events = await stream(runtime_for([run_completed()]), run_input)
        assert types_of(events) == [
            "RUN_STARTED",
            "STATE_SNAPSHOT",
            "STATE_SNAPSHOT",
            "RUN_FINISHED",
        ]
        assert events[1].snapshot == {"cart": []}

    async def test_no_state_events_when_the_client_sent_none(self):
        events = await stream(runtime_for([run_completed()]))
        assert "STATE_SNAPSHOT" not in types_of(events)

    async def test_a_tool_mutation_emits_a_json_patch_delta(self):
        runtime = runtime_for([], sequencer_mode=SequencerMode.AUDIT)

        # The agent mutates the very dict the tracker handed to RunContext,
        # which is what makes the diff visible.
        async def mutating_stream(**kwargs):
            state = kwargs["run_context"].session_state
            yield tool_started("c1", "add_item")
            state["cart"] = ["apple"]
            yield tool_completed("c1", "add_item", "added")
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: mutating_stream(**kwargs)
        events = await stream(runtime, make_input(state={"cart": []}))
        deltas = [e for e in events if e.type is EventType.STATE_DELTA]
        assert deltas, types_of(events)
        assert deltas[0].delta == [{"op": "add", "path": "/cart/0", "value": "apple"}]


class TestStreamUI:
    FENCED = 'Look:\n```stream-ui {"schema": "demo-card"}\n{"type":"stat","value":1}\n```\nDone.\n'

    async def test_the_fence_is_stripped_and_emitted_as_custom_events(self):
        chunks = [content(piece) for piece in _split(self.FENCED, 7)]
        events = await stream(runtime_for([*chunks, run_completed()]))
        assert text_of(events) == "Look:\nDone.\n"
        names = [e.name for e in customs(events)]
        assert names == ["ui.block.start", "ui.item", "ui.block.end"]

    async def test_block_events_carry_the_message_id(self):
        events = await stream(runtime_for([content(self.FENCED), run_completed()]))
        message_ids = {e.value["messageId"] for e in customs(events)}
        starts = [e for e in events if e.type is EventType.TEXT_MESSAGE_START]
        assert message_ids == {starts[0].message_id}

    async def test_a_fence_only_answer_produces_no_text_message(self):
        text = '```stream-ui {"schema": "demo-card"}\n{"only":"ui"}\n```\n'
        events = await stream(runtime_for([content(text), run_completed()]))
        assert "TEXT_MESSAGE_START" not in types_of(events)
        assert len(customs(events)) == 3

    async def test_a2ui_can_be_disabled(self):
        runtime = runtime_for([content(self.FENCED), run_completed()], enable_streamui=False)
        events = await stream(runtime)
        assert customs(events) == []
        assert "stream-ui" in text_of(events)


class TestHITL:
    def _paused(self):
        return run_paused(
            [
                tool_execution(
                    "c1",
                    "delete_file",
                    {"path": "/tmp/x"},
                    requires_confirmation=True,
                )
            ]
        )

    async def test_a_paused_run_renders_the_waiting_tool(self):
        events = await stream(runtime_for([content("May I?"), self._paused()]))
        assert "TOOL_CALL_START" in types_of(events)
        assert types_of(events)[-1] == "RUN_FINISHED"

    async def test_a_pause_descriptor_tells_the_client_what_is_needed(self):
        events = await stream(runtime_for([self._paused()]))
        paused = customs(events, EVENT_RUN_PAUSED)
        assert len(paused) == 1
        assert paused[0].value["pauseType"] == "confirmation"
        assert paused[0].value["toolName"] == "delete_file"
        assert paused[0].value["toolArgs"] == {"path": "/tmp/x"}

    async def test_run_paused_is_emitted_before_the_waiting_tool_frames(self):
        events = await stream(runtime_for([self._paused()]))
        names = [
            getattr(event, "name", None) if event.type == EventType.CUSTOM else event.type.value
            for event in events
        ]
        assert names.index(EVENT_RUN_PAUSED) < names.index("TOOL_CALL_START")

    async def test_a_user_feedback_pause_carries_the_questions(self):
        from agno.tools.function import UserFeedbackQuestion

        events = await stream(
            runtime_for(
                [
                    run_paused(
                        [
                            tool_execution(
                                "c1",
                                "rate",
                                {},
                                user_feedback_schema=[
                                    UserFeedbackQuestion(
                                        question="Was this useful?", header="useful"
                                    )
                                ],
                            )
                        ]
                    )
                ]
            )
        )
        paused = customs(events, EVENT_RUN_PAUSED)
        assert paused[0].value["pauseType"] == "user_feedback"
        assert paused[0].value["userInputSchema"][0]["name"] == "Was this useful?"
        assert paused[0].value["userInputSchema"][0]["description"] == "useful"
        assert paused[0].value["userInputSchema"][0]["fieldType"] == "choice"

    async def test_trailing_tool_messages_route_to_resume(self, monkeypatch):
        from ag_ui.core.types import ToolMessage

        seen = {}

        async def fake_resume(**kwargs):
            seen.update(kwargs)

            async def gen():
                yield content("Resumed.")
                yield run_completed()

            return gen()

        monkeypatch.setattr("agno_harness.runtime.runner.resume_paused_run", fake_resume)

        run_input = make_input(
            messages=[ToolMessage(id="t1", role="tool", tool_call_id="c1", content="true")]
        )
        events = await stream(runtime_for([]), run_input)
        assert text_of(events) == "Resumed."
        assert [m.tool_call_id for m in seen["tool_messages"]] == ["c1"]
        assert seen["session_id"] == "thread-1"
        results = [event for event in events if event.type == EventType.TOOL_CALL_RESULT]
        assert len(results) == 1
        assert results[0].tool_call_id == "c1"
        assert results[0].content == "true"


class TestSubAgents:
    async def test_a_sub_agents_output_arrives_inside_the_bracket(self):
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            yield content("Delegating. ")
            async with substream("reviewer") as emit:
                await emit(content("Reviewing your code."))
            yield content("Done.")
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)
        names = _names(events)
        assert names.index(EVENT_SUBAGENT_START) < names.index(EVENT_SUBAGENT_END)
        assert "Reviewing your code." in text_of(events)

    async def test_the_bracket_carries_no_step_events(self):
        """STEP_* said the same thing the boundaries say, and nothing read it.

        The runtime's own comment told clients to group on the CUSTOM bracket,
        and replay never restored the steps, so they were noise on the wire.
        """
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            async with substream("code_reviewer") as emit:
                await emit(content("hi"))
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)
        assert "STEP_STARTED" not in types_of(events)
        assert "STEP_FINISHED" not in types_of(events)

    async def test_the_bracket_closes_even_if_the_sub_agent_raises(self):
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            try:
                async with substream("flaky") as emit:
                    await emit(content("starting"))
                    raise RuntimeError("sub-agent died")
            except RuntimeError:
                pass
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)
        assert len(customs(events, EVENT_SUBAGENT_END)) == 1

    async def test_disabling_the_feature_hides_the_inner_run(self):
        from agno_harness import substream

        runtime = runtime_for([], enable_subagent_streaming=False)

        async def parent_stream(**kwargs):
            async with substream("reviewer") as emit:
                await emit(content("invisible"))
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)
        assert customs(events, EVENT_SUBAGENT_START) == []
        assert "invisible" not in text_of(events)

    async def test_boundaries_enclose_every_forwarded_frame(self):
        """The frames stay ordinary AG-UI events; the boundaries say whose they are."""
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            yield content("Delegating. ")
            async with substream("reviewer") as emit:
                await emit(content("", reasoning="Reading the code."))
                await emit(content("Looks fine."))
            yield content("Done.")
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)

        names = [
            getattr(e, "name", None) if e.type is EventType.CUSTOM else e.type.value for e in events
        ]
        start = names.index(EVENT_SUBAGENT_START)
        end = names.index(EVENT_SUBAGENT_END)
        inner = names[start:end]
        assert "REASONING_MESSAGE_CONTENT" in inner
        # The sub-agent's own text opens and closes between the boundaries, so a
        # client routing on them keeps it out of the parent's answer.
        assert inner.count("TEXT_MESSAGE_START") == 1
        assert "TEXT_MESSAGE_START" in names[end:]

    async def test_the_task_description_rides_on_the_start_boundary(self):
        """A header wants six words and a delegated task wants everything, so the
        tool sends both and the client picks per view."""
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            async with substream(
                "reviewer",
                description="Reviewing the add helper",
                prompt="def add(a, b): return a - b",
            ) as emit:
                await emit(content("The sign is wrong."))
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)

        value = customs(events, EVENT_SUBAGENT_START)[0].value
        assert value["description"] == "Reviewing the add helper"
        assert value["prompt"] == "def add(a, b): return a - b"

    async def test_an_undescribed_delegation_omits_the_fields(self):
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            async with substream("reviewer") as emit:
                await emit(content("hi"))
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)

        value = customs(events, EVENT_SUBAGENT_START)[0].value
        assert set(value) == {"name", "subRunId"}

    async def test_each_delegation_gets_its_own_sub_run_id(self):
        """Two calls to one reviewer is two runs, not one panel with both inside."""
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            for take in ("first", "second"):
                async with substream("reviewer") as emit:
                    await emit(content(take))
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)

        started = [e.value["subRunId"] for e in customs(events, EVENT_SUBAGENT_START)]
        ended = [e.value["subRunId"] for e in customs(events, EVENT_SUBAGENT_END)]
        assert len(set(started)) == 2
        assert started == ended
        assert all(value.startswith("reviewer-") for value in started)

    async def test_the_tool_can_read_the_id_it_was_given(self):
        """The tool result is the only part of a delegation Agno persists, so a
        tool that wants the link recorded needs the id while it runs."""
        from agno_harness import substream

        seen: list[str] = []

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            async with substream("reviewer") as emit:
                seen.append(emit.sub_run_id)
                await emit(content("hi"))
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)

        assert seen == [customs(events, EVENT_SUBAGENT_START)[0].value["subRunId"]]

    async def test_the_boundary_names_the_tool_call_that_delegated(self):
        """The delegating card is usually hidden, so the id is how a client
        relates the panel back to the call that opened it."""
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            yield tool_started("call-7", "delegate", {"agent_name": "reviewer"})
            async with substream("reviewer") as emit:
                await emit(content("Looks fine."))
            yield tool_completed("call-7", "delegate", "reviewed")
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)

        assert customs(events, EVENT_SUBAGENT_START)[0].value["toolCallId"] == "call-7"

    async def test_parallel_tool_calls_leave_the_link_out(self):
        """Two calls in flight makes the delegator a guess, and a wrong link is
        worse than none."""
        from agno_harness import substream

        runtime = runtime_for([])

        async def parent_stream(**kwargs):
            yield tool_started("call-1", "delegate", {"agent_name": "reviewer"})
            yield tool_started("call-2", "get_weather", {"city": "Tokyo"})
            async with substream("reviewer") as emit:
                await emit(content("Looks fine."))
            yield tool_completed("call-1", "delegate", "reviewed")
            yield tool_completed("call-2", "get_weather", "sunny")
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)

        assert "toolCallId" not in customs(events, EVENT_SUBAGENT_START)[0].value

    async def test_two_delegations_to_one_agent_do_not_share_state(self):
        """Delegating twice to the same agent used to key state by agent name.

        The second delegation overwrote the first's StreamState, so both panels
        minted message ids from one counter and their text ran together into a
        single message.
        """
        from agno_harness import substream

        runtime = runtime_for([])
        second_done = asyncio.Event()
        first_open = asyncio.Event()

        async def parent_stream(**kwargs):
            # Genuinely overlapping. Sequential delegations happen to work even
            # with the old keying, because the second one's state replaces a
            # slot nobody needs any more; nesting is what exposed it.
            async def first() -> None:
                async with substream("reviewer", description="first") as emit:
                    first_open.set()
                    await second_done.wait()
                    await emit(content("one"))

            async def second() -> None:
                await first_open.wait()
                async with substream("reviewer", description="second") as emit:
                    await emit(content("two"))
                second_done.set()

            await asyncio.gather(first(), second())
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)

        started = [e.value["subRunId"] for e in customs(events, EVENT_SUBAGENT_START)]
        ended = [e.value["subRunId"] for e in customs(events, EVENT_SUBAGENT_END)]
        assert len(set(started)) == 2
        assert sorted(ended) == sorted(started)
        # Both sub-agents' text survives. Under the old keying the second
        # delegation took over the first's slot, and the first's remaining
        # output was dropped for want of a state to convert it against.
        assert set(text_of(events)) >= set("onetwo")

    async def test_no_boundaries_when_the_feature_is_disabled(self):
        from agno_harness import substream

        runtime = runtime_for([], enable_subagent_streaming=False)

        async def parent_stream(**kwargs):
            async with substream("reviewer") as emit:
                await emit(content("invisible"))
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = await stream(runtime)
        assert customs(events, EVENT_SUBAGENT_START) == []

    async def test_the_steps_parser_marks_delegation_without_streaming(self):
        runtime = runtime_for(
            [
                tool_started("c1", "delegate", {"agent_name": "reviewer"}),
                tool_completed("c1", "delegate", "reviewed", {"agent_name": "reviewer"}),
                run_completed(),
            ]
        )
        runtime.register_parser("ToolCallStarted", subagent_steps_parser(["delegate"]))
        runtime.register_parser("ToolCallCompleted", subagent_steps_parser(["delegate"]))
        events = await stream(runtime)
        steps = [e for e in events if e.type is EventType.STEP_STARTED]
        assert steps[0].step_name == "→ reviewer"


def _names(events):
    """Event types, with CUSTOM replaced by the custom name."""
    return [
        getattr(e, "name", None) if e.type is EventType.CUSTOM else e.type.value for e in events
    ]


class TestIntrospection:
    async def test_chunk_samples_record_raw_shapes_per_type(self):
        runtime = runtime_for(
            [content("a"), content("b"), content("c"), content("d"), run_completed()],
            record_chunks=2,
        )
        await stream(runtime)
        samples = runtime.chunk_samples()
        by_type = {t["eventType"]: t for t in samples["types"]}
        assert by_type["RunContent"]["count"] == 4
        assert len(by_type["RunContent"]["samples"]) == 2
        assert by_type["RunContent"]["samples"][0]["payload"]["content"] == "a"

    async def test_samples_capture_the_stream_state_at_that_moment(self):
        runtime = runtime_for([tool_started("c1", "f"), run_completed()])
        await stream(runtime)
        by_type = {t["eventType"]: t for t in runtime.chunk_samples()["types"]}
        state = by_type["ToolCallStarted"]["samples"][0]["streamState"]
        assert state["runId"] == "run-1"

    async def test_recording_can_be_disabled(self):
        runtime = runtime_for([content("a"), run_completed()], record_chunks=0)
        await stream(runtime)
        assert runtime.chunk_samples()["types"] == []

    async def test_audit_mode_reports_the_repairs_it_made(self):
        # Agno opens a parent text message to host a tool call. When the tool is
        # the whole turn that message never gets content, and the sequencer
        # discards it -- the repair that makes hidden tools clean.
        runtime = runtime_for(
            [tool_started("c1", "f"), tool_completed("c1", "f", "ok"), run_completed()],
            sequencer_mode=SequencerMode.AUDIT,
        )
        await stream(runtime)
        assert "empty_text_message" in [v.rule for v in runtime.last_violations()]

    async def test_repair_mode_records_nothing(self):
        runtime = runtime_for(
            [tool_started("c1", "f"), tool_completed("c1", "f", "ok"), run_completed()],
            sequencer_mode=SequencerMode.REPAIR,
        )
        await stream(runtime)
        assert runtime.last_violations() == []

    async def test_the_stream_state_is_retrievable_after_a_run(self):
        runtime = runtime_for([content("hi"), run_completed()])
        await stream(runtime)
        assert runtime.stream_state("run-1")["runId"] == "run-1"


class TestClientReducer:
    async def test_a_trace_reduces_to_the_expected_message(self):
        events = await stream(
            runtime_for(
                [
                    content("The answer is "),
                    tool_started("c1", "calc", {"x": 1}),
                    tool_completed("c1", "calc", 42),
                    content("42."),
                    run_completed(),
                ]
            )
        )
        messages = messages_from_events(events)
        assert len(messages) == 1
        assert messages[0]["content"] == "The answer is 42."
        assert [t["name"] for t in messages[0]["toolCalls"]] == ["calc"]


def _split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


@pytest.mark.parametrize("chunk_size", [1, 3, 11, 1000])
async def test_output_is_identical_however_the_text_is_chunked(chunk_size):
    """Delta boundaries must not change what the user sees."""
    answer = 'Intro.\n```stream-ui {"schema": "demo-card"}\n{"a":1}\n{"b":2}\n```\nOutro.'
    chunks = [content(piece) for piece in _split(answer, chunk_size)]
    events = await stream(runtime_for([*chunks, run_completed()]))
    assert text_of(events) == "Intro.\nOutro."
    lines = [e for e in customs(events) if e.name == "ui.item"]
    assert [e.value["data"] for e in lines] == [{"a": 1}, {"b": 2}]


async def test_upstream_stream_read_timeout():
    class StallingAgent:
        def __init__(self):
            self.db = None

        def arun(self, *args, **kwargs):
            return self._stream()

        async def _stream(self):
            yield content("starting...")
            await asyncio.sleep(1.0)
            yield content("never reached")
            yield run_completed()

    runtime = AgentRuntime(agent=StallingAgent(), read_timeout=0.1)
    events = [event async for event in runtime.stream_events(make_input())]
    types = [e.type.value if hasattr(e.type, "value") else str(e.type) for e in events]
    assert "RUN_ERROR" in types
    error_event = next(e for e in events if getattr(e, "type", None) == EventType.RUN_ERROR)
    assert "timed out after 0.1s" in error_event.message
