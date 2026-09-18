"""Sequencer invariants, exercised with deliberately malformed input."""

from __future__ import annotations

import pytest
from ag_ui.core import (
    CustomEvent,
    EventType,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from agno_harness.core.sequencer import (
    EventSequencer,
    ProtocolViolationError,
    SequencerMode,
)

from .conformance import assert_valid_agui_sequence


def run(events, mode=SequencerMode.AUDIT):
    sequencer = EventSequencer(thread_id="t", run_id="r", mode=mode)
    out = []
    for event in events:
        out.extend(sequencer.feed(event))
    out.extend(sequencer.finish())
    return out, sequencer


def types(events):
    return [e.type.value for e in events]


def rules(sequencer):
    return [v.rule for v in sequencer.violations]


START = RunStartedEvent(type=EventType.RUN_STARTED, thread_id="t", run_id="r")
FINISH = RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id="t", run_id="r")


def text(message_id="m1", delta="hello"):
    return [
        TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=message_id),
        TextMessageContentEvent(
            type=EventType.TEXT_MESSAGE_CONTENT, message_id=message_id, delta=delta
        ),
        TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=message_id),
    ]


def reasoning(message_id="r1", deltas=()):
    """A whole block, the way a thinking model announces one."""
    return [
        ReasoningStartEvent(type=EventType.REASONING_START, message_id=message_id),
        ReasoningMessageStartEvent(
            type=EventType.REASONING_MESSAGE_START, message_id=message_id, role="reasoning"
        ),
        *(
            ReasoningMessageContentEvent(
                type=EventType.REASONING_MESSAGE_CONTENT, message_id=message_id, delta=delta
            )
            for delta in deltas
        ),
        ReasoningMessageEndEvent(type=EventType.REASONING_MESSAGE_END, message_id=message_id),
        ReasoningEndEvent(type=EventType.REASONING_END, message_id=message_id),
    ]


class TestHappyPath:
    def test_well_formed_stream_passes_through_unchanged(self):
        events = [START, *text(), FINISH]
        out, sequencer = run(events)
        assert types(out) == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]
        assert sequencer.violations == []

    def test_strict_mode_accepts_a_well_formed_stream(self):
        out, _ = run([START, *text(), FINISH], mode=SequencerMode.STRICT)
        assert_valid_agui_sequence(out)


class TestRunLifecycle:
    def test_pre_run_events_are_released_after_run_started(self):
        billing = CustomEvent(type=EventType.CUSTOM, name="billing", value={"cost": 1})
        out, sequencer = run([billing, START, FINISH])
        assert types(out) == ["RUN_STARTED", "CUSTOM", "RUN_FINISHED"]
        assert rules(sequencer) == ["before_run_started"]

    def test_frames_after_the_terminal_event_are_dropped(self):
        out, sequencer = run([START, FINISH, *text()])
        assert types(out) == ["RUN_STARTED", "RUN_FINISHED"]
        assert set(rules(sequencer)) == {"after_terminal"}

    def test_duplicate_run_started_is_dropped(self):
        out, sequencer = run([START, START, FINISH])
        assert types(out).count("RUN_STARTED") == 1
        assert "duplicate_run_started" in rules(sequencer)

    def test_run_error_terminates_and_closes_open_scaffolding(self):
        error = RunErrorEvent(type=EventType.RUN_ERROR, message="boom")
        out, _ = run(
            [
                START,
                TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id="m1"),
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id="m1", delta="partial"
                ),
                error,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_ERROR",
        ]
        assert_valid_agui_sequence(out)

    def test_orphan_pre_run_events_still_get_a_run_started(self):
        billing = CustomEvent(type=EventType.CUSTOM, name="billing", value={})
        out, sequencer = run([billing])
        assert types(out) == ["RUN_STARTED", "CUSTOM"]
        assert "run_started_missing" in rules(sequencer)


class TestTextMessages:
    def test_empty_start_end_pair_never_reaches_the_wire(self):
        out, sequencer = run([START, *text(delta="")[:1], text()[2], FINISH])
        assert types(out) == ["RUN_STARTED", "RUN_FINISHED"]
        assert "empty_text_message" in rules(sequencer)

    def test_content_without_start_opens_a_message(self):
        out, sequencer = run(
            [
                START,
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id="m9", delta="hi"
                ),
                FINISH,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]
        assert "content_before_start" in rules(sequencer)

    def test_empty_deltas_are_dropped(self):
        out, sequencer = run(
            [
                START,
                TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id="m1"),
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id="m1", delta=""
                ),
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id="m1", delta="real"
                ),
                TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id="m1"),
                FINISH,
            ]
        )
        assert types(out).count("TEXT_MESSAGE_CONTENT") == 1
        assert "empty_text_content" in rules(sequencer)

    def test_overlapping_messages_close_the_previous_one(self):
        out, sequencer = run(
            [
                START,
                *text("m1", "one")[:2],
                *text("m2", "two")[:2],
                FINISH,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]
        assert "overlapping_text_message" in rules(sequencer)

    def test_content_for_the_wrong_message_is_retargeted(self):
        out, sequencer = run(
            [
                START,
                *text("m1", "one")[:2],
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id="stray", delta="two"
                ),
                FINISH,
            ]
        )
        contents = [e for e in out if e.type is EventType.TEXT_MESSAGE_CONTENT]
        assert {e.message_id for e in contents} == {"m1"}
        assert "text_message_id_mismatch" in rules(sequencer)

    def test_end_without_start_is_dropped(self):
        out, sequencer = run(
            [START, TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id="m1"), FINISH]
        )
        assert types(out) == ["RUN_STARTED", "RUN_FINISHED"]
        assert "end_without_start" in rules(sequencer)


class TestToolCalls:
    def test_unclosed_tool_call_is_closed_before_the_terminal_event(self):
        out, sequencer = run(
            [
                START,
                ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="f"
                ),
                FINISH,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
            "TOOL_CALL_START",
            "TOOL_CALL_END",
            "RUN_FINISHED",
        ]
        assert "unclosed_tool_call" in rules(sequencer)

    def test_args_without_start_synthesize_one(self):
        out, sequencer = run(
            [
                START,
                ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id="c1", delta="{}"),
                FINISH,
            ]
        )
        assert types(out)[:3] == ["RUN_STARTED", "TOOL_CALL_START", "TOOL_CALL_ARGS"]
        assert "tool_event_without_start" in rules(sequencer)

    def test_result_before_end_emits_the_missing_end(self):
        out, sequencer = run(
            [
                START,
                ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="f"
                ),
                ToolCallResultEvent(
                    type=EventType.TOOL_CALL_RESULT,
                    tool_call_id="c1",
                    message_id="c1",
                    content="ok",
                ),
                FINISH,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
            "TOOL_CALL_START",
            "TOOL_CALL_END",
            "TOOL_CALL_RESULT",
            "RUN_FINISHED",
        ]
        assert "result_before_end" in rules(sequencer)

    def test_duplicate_result_is_dropped(self):
        result = ToolCallResultEvent(
            type=EventType.TOOL_CALL_RESULT, tool_call_id="c1", message_id="c1", content="ok"
        )
        out, sequencer = run(
            [
                START,
                ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="f"
                ),
                ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id="c1"),
                result,
                result,
                FINISH,
            ]
        )
        assert types(out).count("TOOL_CALL_RESULT") == 1
        assert "duplicate_tool_result" in rules(sequencer)

    def test_a_tool_call_closes_an_open_text_message_first(self):
        out, _ = run(
            [
                START,
                *text("m1", "thinking out loud")[:2],
                ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="f"
                ),
                ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id="c1"),
                FINISH,
            ]
        )
        assert types(out).index("TEXT_MESSAGE_END") < types(out).index("TOOL_CALL_START")
        assert_valid_agui_sequence(out)


class TestReasoning:
    def test_reasoning_content_alone_builds_the_whole_session(self):
        out, sequencer = run(
            [
                START,
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r1", delta="hmm"
                ),
                FINISH,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
            "REASONING_START",
            "REASONING_MESSAGE_START",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_END",
            "REASONING_END",
            "RUN_FINISHED",
        ]
        assert "reasoning_event_without_start" in rules(sequencer)

    def test_a_block_that_says_nothing_never_ships(self):
        """Thinking models announce a block and then stream nothing into it.

        Shipped as-is that is an empty bubble in the transcript, so it is held
        back and dropped — the same rule text messages already live by.
        """
        out, sequencer = run([START, *reasoning(), FINISH])
        assert types(out) == ["RUN_STARTED", "RUN_FINISHED"]
        assert "empty_reasoning_block" in rules(sequencer)

    def test_a_block_of_pure_whitespace_never_ships_either(self):
        out, _ = run([START, *reasoning(deltas=["\n", "  "]), FINISH])
        assert types(out) == ["RUN_STARTED", "RUN_FINISHED"]

    def test_the_held_frames_arrive_in_order_once_it_speaks(self):
        out, _ = run([START, *reasoning(deltas=["hmm"]), FINISH])
        assert types(out) == [
            "RUN_STARTED",
            "REASONING_START",
            "REASONING_MESSAGE_START",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_END",
            "REASONING_END",
            "RUN_FINISHED",
        ]

    def test_a_leading_newline_is_kept_rather_than_dropped(self):
        """Held, not discarded: it is the paragraph break before the thought."""
        out, _ = run([START, *reasoning(deltas=["\n", "hmm"]), FINISH])
        deltas = [e.delta for e in out if e.type is EventType.REASONING_MESSAGE_CONTENT]
        assert deltas == ["\n", "hmm"]

    def test_an_empty_block_does_not_end_the_answer_it_interrupts(self):
        """The reason this matters beyond tidiness.

        A reasoning block forces the open text message closed, so an empty one
        arriving mid-answer splits it in two and breaks anything spanning the
        seam — a StreamUI fence above all.
        """
        out, _ = run(
            [
                START,
                TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id="m1"),
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id="m1", delta="before "
                ),
                *reasoning(deltas=["\n"]),
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id="m1", delta="after"
                ),
                TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id="m1"),
                FINISH,
            ]
        )
        assert types(out).count("TEXT_MESSAGE_START") == 1
        assert "".join(e.delta for e in out if e.type is EventType.TEXT_MESSAGE_CONTENT) == (
            "before after"
        )

    def test_reasoning_closes_when_the_answer_starts(self):
        """Agno leaves it open to the end, which strands REASONING_END past the answer."""
        out, _ = run(
            [
                START,
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r1", delta="hmm"
                ),
                *text(delta="The answer."),
                FINISH,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
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

    def test_think_call_think_answer_stay_four_separate_blocks(self):
        """Reasoning either side of a tool call is two sessions, not one."""
        out, _ = run(
            [
                START,
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r1", delta="hmm"
                ),
                # Agno opens a text message purely to host the tool call, then
                # closes it without ever putting content in it.
                TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id="host"),
                ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="search"
                ),
                ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id="c1"),
                TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id="host"),
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r2", delta="now I know"
                ),
                *text(delta="Done."),
                FINISH,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
            "REASONING_START",
            "REASONING_MESSAGE_START",
            "REASONING_MESSAGE_CONTENT",
            "REASONING_MESSAGE_END",
            "REASONING_END",
            "TOOL_CALL_START",
            "TOOL_CALL_END",
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

    def test_the_two_reasoning_sessions_get_distinct_ids(self):
        """A client keying blocks by message id must not merge them."""
        out, _ = run(
            [
                START,
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r1", delta="before"
                ),
                ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="search"
                ),
                ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id="c1"),
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r2", delta="after"
                ),
                FINISH,
            ]
        )
        ids = [e.message_id for e in out if e.type is EventType.REASONING_MESSAGE_CONTENT]
        assert ids == ["r1", "r2"]

    def test_a_reused_reasoning_id_is_remapped(self):
        """Agno keeps one reasoning id per run, so both sessions arrive as ``r1``."""
        out, sequencer = run(
            [
                START,
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r1", delta="before"
                ),
                ToolCallStartEvent(
                    type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="search"
                ),
                ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id="c1"),
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r1", delta="after"
                ),
                FINISH,
            ]
        )
        first, second = [e.message_id for e in out if e.type is EventType.REASONING_MESSAGE_CONTENT]
        assert first == "r1"
        assert second != first
        assert "reasoning_id_reused" in rules(sequencer)
        # Every frame of a session agrees with its content, so a client can pair
        # START / CONTENT / END without guessing.
        reasoning_ids = [
            e.message_id
            for e in out
            if e.type
            in (
                EventType.REASONING_START,
                EventType.REASONING_MESSAGE_START,
                EventType.REASONING_MESSAGE_CONTENT,
                EventType.REASONING_MESSAGE_END,
                EventType.REASONING_END,
            )
        ]
        assert reasoning_ids == [first] * 5 + [second] * 5

    def test_a_third_session_gets_a_third_id(self):
        out, _ = run(
            [
                START,
                *[
                    event
                    for index in range(3)
                    for event in (
                        ReasoningMessageContentEvent(
                            type=EventType.REASONING_MESSAGE_CONTENT,
                            message_id="r1",
                            delta=f"think {index}",
                        ),
                        ToolCallStartEvent(
                            type=EventType.TOOL_CALL_START,
                            tool_call_id=f"c{index}",
                            tool_call_name="search",
                        ),
                        ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=f"c{index}"),
                    )
                ],
                FINISH,
            ]
        )
        ids = [e.message_id for e in out if e.type is EventType.REASONING_MESSAGE_CONTENT]
        assert len(set(ids)) == 3

    def test_reasoning_after_the_answer_opens_a_fresh_session(self):
        out, _ = run(
            [
                START,
                *text(message_id="m1", delta="First."),
                ReasoningMessageContentEvent(
                    type=EventType.REASONING_MESSAGE_CONTENT, message_id="r2", delta="rethink"
                ),
                *text(message_id="m2", delta="Second."),
                FINISH,
            ]
        )
        assert types(out) == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
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


class TestSteps:
    def test_unfinished_steps_are_closed(self):
        out, sequencer = run(
            [START, StepStartedEvent(type=EventType.STEP_STARTED, step_name="s"), FINISH]
        )
        assert types(out) == ["RUN_STARTED", "STEP_STARTED", "STEP_FINISHED", "RUN_FINISHED"]
        assert "unclosed_step" in rules(sequencer)

    def test_step_finished_without_start_is_dropped(self):
        out, sequencer = run(
            [START, StepFinishedEvent(type=EventType.STEP_FINISHED, step_name="s"), FINISH]
        )
        assert types(out) == ["RUN_STARTED", "RUN_FINISHED"]
        assert "step_finished_without_start" in rules(sequencer)


class TestModes:
    def test_strict_mode_raises_on_the_first_violation(self):
        sequencer = EventSequencer(mode=SequencerMode.STRICT)
        sequencer.feed(START)
        with pytest.raises(ProtocolViolationError) as excinfo:
            sequencer.feed(
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT, message_id="m", delta="x"
                )
            )
        assert excinfo.value.violation.rule == "content_before_start"

    def test_repair_mode_fixes_without_recording(self):
        out, sequencer = run([START, FINISH, *text()], mode=SequencerMode.REPAIR)
        assert types(out) == ["RUN_STARTED", "RUN_FINISHED"]
        assert sequencer.violations == []


def test_repaired_output_is_always_conformant():
    """Whatever malformed input goes in, valid output comes out."""
    garbage = [
        TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id="x", delta="a"),
        START,
        TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id="m1"),
        TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id="m2"),
        ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id="c1", delta="{"),
        ToolCallResultEvent(
            type=EventType.TOOL_CALL_RESULT, tool_call_id="c2", message_id="c2", content="?"
        ),
        ReasoningMessageContentEvent(
            type=EventType.REASONING_MESSAGE_CONTENT, message_id="r", delta="mm"
        ),
        StepFinishedEvent(type=EventType.STEP_FINISHED, step_name="ghost"),
        FINISH,
        FINISH,
    ]
    out, sequencer = run(garbage)
    assert sequencer.violations, "expected the garbage input to trip invariants"
    assert_valid_agui_sequence(out)
