"""Chunk sequences copied from real model runs that broke something.

Every case here was a bug report first. The point of the file is that adding the
next one is cheap — paste the chunks and the answer they should have produced —
and that the shared invariants below then guard *all* of them on every run,
including the ones nobody thought to assert.

The quirks are all the same family: a thinking model dribbles reasoning between
content chunks, and Agno's handler closes the open text message before every
reasoning delta. A blank thought therefore costs an answer, because the model
reopens it under a new message id — and a StreamUI fence opened in the first
half and closed in the second parses as neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from ag_ui.core import BaseEvent, EventType

from agno_relay import AguiRuntime, SequencerMode
from agno_relay.core.streamui import BlockSchema, CardCatalog, ItemSchema

from .conformance import assert_valid_agui_sequence
from .conftest import FakeAgent, collect, content, customs, make_input, run_completed


class Row(ItemSchema):
    schema_name = "row"
    cells: list[str]


class Table(BlockSchema):
    schema_name = "table"
    item = Row
    columns: list[str]


class CodeCard(BlockSchema):
    schema_name = "code-card"
    body = "text"
    lang: str


def catalog() -> CardCatalog:
    return CardCatalog([Table, CodeCard])


@dataclass
class Quirk:
    """One recorded misbehaviour and the output it should not have prevented."""

    chunks: list
    text: str = ""
    thoughts: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    card_text: str | None = None


# The Python listing from the code-card report, backslashes and all: a body that
# is not JSON, inside a fence, arriving in pieces.
CODE = (
    "import re\n\n"
    'def read_windows_path(text: str) -> str | None:\n    """Extract a path."""\n'
    '    match = re.search(r"[A-Za-z]:\\\\[^\\s<>|?*\\"]+", text)\n'
    "    return match.group() if match else None\n"
)


QUIRKS: dict[str, Quirk] = {
    "blank_reasoning_splits_an_answer": Quirk(
        # Qwen sprinkles a lone newline of reasoning between content chunks.
        chunks=[
            content("Here is "),
            content("", reasoning="\n"),
            content("the answer."),
            run_completed(),
        ],
        text="Here is the answer.",
    ),
    "blank_reasoning_splits_a_fence": Quirk(
        # The same newline, landing between a fence header and its body.
        chunks=[
            content('```stream-ui {"schema": "table", "columns": ["City"]}\n'),
            content("", reasoning="\n"),
            content('{"cells": ["Tokyo"]}\n```\n'),
            run_completed(),
        ],
        items=[{"cells": ["Tokyo"]}],
    ),
    "blank_reasoning_after_a_real_thought": Quirk(
        # The nastiest one, and the reason "has Agno opened a reasoning session"
        # is the wrong question to ask: Agno keeps one id for the whole run and
        # never clears it, so after the first thought that flag says "thinking"
        # for the rest of the run, answer or no answer.
        chunks=[
            content("", reasoning="I will write a code card."),
            content("\n\n"),
            content("```"),
            content("", reasoning="\n"),
            content(f'stream-ui {{"schema": "code-card", "lang": "python"}}\n{CODE}```\n'),
            run_completed(),
        ],
        text="\n\n",
        thoughts=["I will write a code card."],
        card_text=CODE,
    ),
    "a_thought_that_never_arrives": Quirk(
        # The model announces thinking and streams nothing into it.
        chunks=[
            content("", reasoning="\n"),
            content("Straight to the answer."),
            run_completed(),
        ],
        text="Straight to the answer.",
    ),
    "newlines_inside_a_real_thought_survive": Quirk(
        # The other side of the rule: paragraph breaks in a genuine thought are
        # part of it, and dropping them would be its own bug.
        chunks=[
            content("", reasoning="First."),
            content("", reasoning="\n\n"),
            content("", reasoning="Second."),
            content("Done."),
            run_completed(),
        ],
        text="Done.",
        thoughts=["First.\n\nSecond."],
    ),
}


async def run(quirk: Quirk) -> list[BaseEvent]:
    runtime = AguiRuntime(
        agent=FakeAgent(quirk.chunks),
        catalog=catalog(),
        sequencer_mode=SequencerMode.AUDIT,
    )
    return await collect(runtime.stream_events(make_input()))


def text_messages(events: list[BaseEvent]) -> list[str]:
    """The visible answer, one entry per message the client would draw."""
    messages: list[str] = []
    for event in events:
        if event.type is EventType.TEXT_MESSAGE_START:
            messages.append("")
        elif event.type is EventType.TEXT_MESSAGE_CONTENT:
            messages[-1] += event.delta
    return messages


def thoughts(events: list[BaseEvent]) -> list[str]:
    blocks: list[str] = []
    for event in events:
        if event.type is EventType.REASONING_MESSAGE_START:
            blocks.append("")
        elif event.type is EventType.REASONING_MESSAGE_CONTENT:
            blocks[-1] += event.delta
    return blocks


@pytest.mark.parametrize("name", sorted(QUIRKS))
class TestRecordedQuirks:
    async def test_the_answer_is_not_torn_in_two(self, name):
        """One uninterrupted answer arrives as one message.

        None of these runs has a tool call or a real thought in the middle of
        the answer, so there is nothing that could legitimately end it early.
        """
        events = await run(QUIRKS[name])
        assert len(text_messages(events)) <= 1

    async def test_the_answer_reads_as_it_was_written(self, name):
        events = await run(QUIRKS[name])
        assert "".join(text_messages(events)) == QUIRKS[name].text

    async def test_no_empty_thinking_block_reaches_the_client(self, name):
        events = await run(QUIRKS[name])
        assert all(block.strip() for block in thoughts(events))

    async def test_the_thinking_that_is_real_survives_intact(self, name):
        events = await run(QUIRKS[name])
        assert thoughts(events) == QUIRKS[name].thoughts

    async def test_the_cards_come_out_whole(self, name):
        events = await run(QUIRKS[name])
        quirk = QUIRKS[name]
        assert [item.value["data"] for item in customs(events, "ui.item")] == quirk.items
        if quirk.card_text is not None:
            assert "".join(e.value["delta"] for e in customs(events, "ui.text")) == quirk.card_text

    async def test_no_fence_marker_is_left_in_the_visible_text(self, name):
        """A card that failed to parse shows up as a bare ``` in the bubble."""
        events = await run(QUIRKS[name])
        assert "```" not in "".join(text_messages(events))

    async def test_the_stream_is_still_a_valid_agui_sequence(self, name):
        assert_valid_agui_sequence(await run(QUIRKS[name]))
