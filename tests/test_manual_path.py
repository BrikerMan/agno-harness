"""Driving the translator by hand, without the managed runtime.

This is the path a caller takes when they want the event pipeline but not the
orchestration: their own loop, their own execution strategy, their own chance to
inspect and rewrite every event before it goes anywhere. It exists so that
wanting one unusual thing does not mean forking the runtime.

These tests are the contract for that path. If the translator ever grows a
dependency on the runtime object, on a request, or on FastAPI, they break.
"""

from __future__ import annotations

import json

from ag_ui.core import CustomEvent, EventType
from agno.run.agent import RunErrorEvent as AgnoRunError

from agno_relay import (
    AgentRunner,
    EventTranslator,
    SequencerMode,
    make_run_scope,
)
from agno_relay.runtime.translator import AgentRunFailed, _repair_ascii_json

from .conformance import assert_valid_agui_sequence
from .conftest import (
    FakeAgent,
    content,
    make_input,
    run_completed,
    text_of,
    tool_completed,
    tool_started,
    types_of,
)


def translator_for(scope, **kwargs):
    kwargs.setdefault("sequencer_mode", SequencerMode.STRICT)
    return EventTranslator(scope=scope, **kwargs)


async def _collect(agent: FakeAgent) -> list:
    scope = make_run_scope(thread_id="t1", run_id="r1")
    translator = EventTranslator(scope=scope)
    events = []
    async for event in translator.start():
        events.append(event)
    async for chunk in agent.arun():
        async for event in translator.feed(chunk):
            events.append(event)
    async for event in translator.finish():
        events.append(event)
    return events


async def test_a_run_can_be_driven_chunk_by_chunk() -> None:
    """The shape the README documents: start, feed, finish."""
    agent = FakeAgent([content("Hello, "), content("world."), run_completed()])
    scope = make_run_scope(thread_id="t1", run_id="r1")
    translator = translator_for(scope)

    events = []
    async for event in translator.start():
        events.append(event)
    async for chunk in agent.arun():
        async for event in translator.feed(chunk):
            events.append(event)
    async for event in translator.finish():
        events.append(event)

    assert_valid_agui_sequence(events)
    assert types_of(events) == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert text_of(events) == "Hello, world."


async def test_events_can_be_rewritten_between_the_translator_and_the_wire() -> None:
    """The reason to drive it yourself: a place to stand in the middle."""
    agent = FakeAgent([content("hi"), run_completed()])
    scope = make_run_scope(thread_id="t1", run_id="r1")
    translator = translator_for(scope)

    seen = []
    async for event in translator.start():
        seen.append(event)
    async for chunk in agent.arun():
        async for event in translator.feed(chunk):
            if event.type is EventType.TEXT_MESSAGE_CONTENT:
                event = event.model_copy(update={"delta": event.delta.upper()})
            seen.append(event)
    async for event in translator.finish():
        seen.append(event)

    assert text_of(seen) == "HI"


async def test_an_event_can_be_injected_without_breaking_the_protocol() -> None:
    agent = FakeAgent([content("hi"), run_completed()])
    scope = make_run_scope(thread_id="t1", run_id="r1")
    translator = translator_for(scope)

    events = []
    async for event in translator.start():
        events.append(event)
    async for event in translator.inject(
        CustomEvent(type=EventType.CUSTOM, name="notice", value={"seat": "free"})
    ):
        events.append(event)
    async for chunk in agent.arun():
        async for event in translator.feed(chunk):
            events.append(event)
    async for event in translator.finish():
        events.append(event)

    assert_valid_agui_sequence(events)
    assert types_of(events)[:2] == ["RUN_STARTED", "CUSTOM"]


async def test_a_failed_run_is_the_callers_to_report() -> None:
    """``feed`` raises rather than inventing a terminal event of its own.

    The managed runtime turns this into ``RUN_ERROR``; a caller driving the
    translator by hand might instead retry, or fall back to another model.
    """
    agent = FakeAgent([AgnoRunError(content="upstream 503")])
    scope = make_run_scope(thread_id="t1", run_id="r1")
    translator = translator_for(scope)

    events = []
    async for event in translator.start():
        events.append(event)

    failure: AgentRunFailed | None = None
    try:
        async for chunk in agent.arun():
            async for event in translator.feed(chunk):
                events.append(event)
    except AgentRunFailed as exc:
        failure = exc

    assert failure is not None
    assert str(failure) == "upstream 503"

    async for event in translator.fail(failure):
        events.append(event)
    for event in translator.close():
        events.append(event)

    assert types_of(events) == ["RUN_STARTED", "RUN_ERROR"]


async def test_the_runner_can_be_used_without_the_runtime() -> None:
    """The other half on its own: chunks out, no event pipeline involved."""
    agent = FakeAgent([content("hi"), run_completed()])
    runner = AgentRunner(agent)
    scope = make_run_scope(thread_id="t1", run_id="r1", user_id="u1")

    chunks = [chunk async for chunk in runner.run(make_input(), scope)]

    assert len(chunks) == 2
    assert agent.last_kwargs["session_id"] == "t1"
    assert agent.last_kwargs["user_id"] == "u1"
    assert scope.run_context.run_id == "r1"


async def test_the_run_is_still_inspectable() -> None:
    """Debug state hangs off the scope, so it survives the manual path."""
    agent = FakeAgent([content("hi"), run_completed()])
    scope = make_run_scope(thread_id="t1", run_id="r1")
    translator = translator_for(scope)

    async for _ in translator.start():
        pass
    async for chunk in agent.arun():
        async for _ in translator.feed(chunk):
            pass
    async for _ in translator.finish():
        pass

    report = scope.inspector.as_dict()
    assert report["runId"] == "r1"
    assert {t["eventType"] for t in report["chunks"]["types"]} == {"RunContent", "RunCompleted"}


async def test_plain_string_tool_results_are_unwrapped_on_the_wire() -> None:
    """Agno json-dumps plain-text results (ensure_ascii=True); we unwrap one layer.

    Without the repair, CJK file content reaches the client as a quoted JSON
    string literal full of ``\\uXXXX`` escapes — one line of mojibake.
    """
    raw = "---\ntype: meeting\nidentity: 视角\n标题: 廊坊赵博士\n"
    agent = FakeAgent(
        [
            tool_started("c1", "read_file"),
            # What Agno's bridge puts on the wire for a plain-text result.
            tool_completed("c1", "read_file", result=json.dumps(raw)),
            run_completed(),
        ]
    )
    scope = make_run_scope(thread_id="t1", run_id="r1")
    # REPAIR (the production mode): Agno's empty parent text message around a
    # tool call is a known upstream quirk the sequencer repairs.
    translator = EventTranslator(scope=scope)

    events = []
    async for event in translator.start():
        events.append(event)
    async for chunk in agent.arun():
        async for event in translator.feed(chunk):
            events.append(event)
    async for event in translator.finish():
        events.append(event)

    results = [e for e in events if e.type is EventType.TOOL_CALL_RESULT]
    assert len(results) == 1
    assert results[0].content == raw


async def test_structured_tool_results_keep_cjk() -> None:
    """Agno dumps objects with ensure_ascii=True; the wire must show real CJK."""
    payload = json.dumps({"status": "success", "chars": 3, "note": "中文"})
    assert r"\u" in payload
    agent = FakeAgent(
        [
            tool_started("c1", "crawl_url"),
            tool_completed("c1", "crawl_url", result=payload),
            run_completed(),
        ]
    )
    scope = make_run_scope(thread_id="t1", run_id="r1")
    translator = EventTranslator(scope=scope)

    events = []
    async for event in translator.start():
        events.append(event)
    async for chunk in agent.arun():
        async for event in translator.feed(chunk):
            events.append(event)
    async for event in translator.finish():
        events.append(event)

    results = [e for e in events if e.type is EventType.TOOL_CALL_RESULT]
    assert len(results) == 1
    assert results[0].content == json.dumps(
        {"status": "success", "chars": 3, "note": "中文"}, ensure_ascii=False
    )
    assert "中文" in results[0].content
    assert r"\u" not in results[0].content


async def test_double_encoded_json_tool_results_are_unwrapped() -> None:
    """A tool that dumps JSON, then Agno dumps that string, still shows CJK."""
    inner = json.dumps({"note": "中文"})
    wrapped = json.dumps(inner)
    agent = FakeAgent(
        [
            tool_started("c1", "crawl_url"),
            tool_completed("c1", "crawl_url", result=wrapped),
            run_completed(),
        ]
    )
    events = await _collect(agent)
    results = [e for e in events if e.type is EventType.TOOL_CALL_RESULT]
    assert len(results) == 1
    assert results[0].content == json.dumps({"note": "中文"}, ensure_ascii=False)


def test_complete_json_args_keep_cjk() -> None:
    ascii_json = json.dumps({"city": "东京"})
    assert r"\u" in ascii_json
    repaired = _repair_ascii_json(ascii_json, unwrap_string=False)
    assert repaired == json.dumps({"city": "东京"}, ensure_ascii=False)


def test_partial_args_delta_is_not_rewritten() -> None:
    fragment = '{"city": "'
    assert _repair_ascii_json(fragment, unwrap_string=False) == fragment
    quoted = '"Tokyo"'
    assert _repair_ascii_json(quoted, unwrap_string=False) == quoted
