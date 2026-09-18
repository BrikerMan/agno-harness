"""Golden traces — byte-for-byte snapshots of each scenario's event stream.

The other tests assert on properties ("the fence is gone", "the tool is
hidden"). These assert on the exact frame sequence, which is what catches the
subtler regressions: a reordering, an extra empty frame, a lost field. Anything
that changes the wire format shows up here as a diff.

Regenerate deliberately with ``UPDATE_GOLDEN=1 uv run pytest tests/test_golden_traces.py``
and read the diff before committing it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from agno_harness import AgentRuntime, HideToolFilter, SequencerMode

from .conformance import assert_valid_agui_sequence, to_jsonl
from .conftest import (
    FakeAgent,
    collect,
    content,
    make_input,
    run_completed,
    tool_completed,
    tool_started,
)

GOLDEN_DIR = Path(__file__).parent / "golden"

# Ids are random per run, so normalize them before comparing.
_ID_PATTERNS = (
    (re.compile(r'"message_id": "[^"]+"'), '"message_id": "<id>"'),
    (re.compile(r'"parent_message_id": "[^"]+"'), '"parent_message_id": "<id>"'),
    (re.compile(r'"tool_call_id": "[^"]+"'), '"tool_call_id": "<id>"'),
    (re.compile(r'"blockId": "[^"]+"'), '"blockId": "<id>"'),
    (re.compile(r'"messageId": "[^"]+"'), '"messageId": "<id>"'),
)


def normalize(trace: str) -> str:
    for pattern, replacement in _ID_PATTERNS:
        trace = pattern.sub(replacement, trace)
    return trace


SCENARIOS = {
    "plain_text": ([content("Hello, "), content("world."), run_completed()], []),
    "reasoning": (
        [content("", reasoning="Let me think. "), content("The answer."), run_completed()],
        [],
    ),
    "single_tool": (
        [
            tool_started("c1", "get_weather", {"city": "Tokyo"}),
            tool_completed("c1", "get_weather", {"tempC": 22}),
            content("It is 22 degrees in Tokyo."),
            run_completed(),
        ],
        [],
    ),
    "parallel_tools": (
        [
            tool_started("c1", "get_weather", {"city": "Tokyo"}),
            tool_started("c2", "get_weather", {"city": "Osaka"}),
            tool_completed("c1", "get_weather", {"tempC": 22}),
            tool_completed("c2", "get_weather", {"tempC": 25}),
            content("Tokyo 22, Osaka 25."),
            run_completed(),
        ],
        [],
    ),
    "hidden_tool": (
        [
            tool_started("c1", "load_skill", {"name": "weather"}),
            tool_completed("c1", "load_skill", "loaded"),
            content("Ready."),
            run_completed(),
        ],
        [HideToolFilter(["load_skill"])],
    ),
    "streamui_fence": (
        [
            content('Here are the stats:\n\n```stream-ui {"schema": "demo-card"}\n'),
            content('{"type":"stat","label":"Stars","value":"15.2k"}\n'),
            content('{"type":"note","text":"Updated hourly."}\n```\n'),
            content("Anything else?"),
            run_completed(),
        ],
        [],
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenario_matches_its_golden_trace(name):
    chunks, filters = SCENARIOS[name]
    runtime = AgentRuntime(agent=FakeAgent(chunks), sequencer_mode=SequencerMode.AUDIT)
    for tool_filter in filters:
        runtime.register_tool_filter(tool_filter)

    events = await collect(runtime.stream_events(make_input()))
    assert_valid_agui_sequence(events)
    actual = normalize(to_jsonl(events))

    path = GOLDEN_DIR / f"{name}.jsonl"
    if os.getenv("UPDATE_GOLDEN") or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        if not os.getenv("UPDATE_GOLDEN"):
            pytest.fail(f"Created missing golden file {path.name}; re-run to verify.")
        return

    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"Trace for {name!r} changed.\n"
        f"Review the diff, then regenerate with:\n"
        f"  UPDATE_GOLDEN=1 uv run pytest tests/test_golden_traces.py"
    )
