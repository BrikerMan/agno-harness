"""StreamUI: the fence parser, the catalog, resolvers, and the tool-side emit.

The streaming tests matter more than they look. Deltas arrive at arbitrary byte
boundaries, so every one of these inputs gets chopped at every position and the
result has to be identical — a fence that only works when the model happens to
emit whole lines is a fence that breaks in production and nowhere else.
"""

from __future__ import annotations

import random
from typing import Literal

import pytest

from agno_relay.core.streamui import (
    EVENT_BLOCK_END,
    EVENT_BLOCK_START,
    EVENT_ITEM,
    EVENT_TEXT,
    BlockSchema,
    CardCatalog,
    CardSchema,
    CatalogConflict,
    ItemSchema,
    MediaUrl,
    StreamUIFenceParser,
    parse_streamui_text,
)


class MovieRef(ItemSchema):
    """One film the model picked, by id."""

    schema_name = "movie-card-item"
    id: int


class MovieList(BlockSchema):
    """A row of film cards."""

    schema_name = "movie-list"
    item = MovieRef
    title: str | None = None


class Note(ItemSchema):
    schema_name = "note"
    text: str


class Mixed(BlockSchema):
    """A block whose lines each name their own schema."""

    schema_name = "mixed"


class CodeCard(BlockSchema):
    """A code listing with a filename and highlighted lines."""

    schema_name = "code-card"
    body = "text"
    lang: str
    path: str | None = None
    highlight: list[int] = []


class Weather(CardSchema):
    """Current conditions for one city."""

    schema_name = "weather"
    city: str
    temp_c: float
    condition: Literal["clear", "cloudy", "rain", "snow", "storm", "fog"]
    icon: MediaUrl | None = None


def catalog(**kwargs) -> CardCatalog:
    return CardCatalog([MovieList, Mixed, CodeCard, Weather], **kwargs)


FENCED = (
    "Here are the results:\n\n"
    '```stream-ui {"schema": "movie-list", "title": "Sci-fi"}\n'
    '{"id": 603}\n'
    '{"id": 27205}\n'
    "```\n"
    "Anything else?\n"
)


def drive(chunks: list[str], cat: CardCatalog | None = None):
    """Feed chunks through a parser and return visible text plus events."""
    parser = StreamUIFenceParser(cat if cat is not None else catalog())
    visible: list[str] = []
    events: list[tuple[str, dict]] = []
    for chunk in chunks:
        text, produced = parser.feed(chunk)
        visible.append(text)
        events.extend((e.kind, e.payload) for e in produced)
    text, produced = parser.flush()
    visible.append(text)
    events.extend((e.kind, e.payload) for e in produced)
    return "".join(visible), events


def normalize(events):
    """Drop the random block id so traces from different runs compare equal."""
    return [
        (kind, {k: v for k, v in payload.items() if k != "blockId"}) for kind, payload in events
    ]


def kinds(events):
    return [kind for kind, _ in events]


def payloads(events, kind):
    return [payload for k, payload in events if k == kind]


class TestExtraction:
    def test_the_fence_never_reaches_the_visible_text(self):
        visible, _ = drive([FENCED])
        assert visible == "Here are the results:\n\nAnything else?\n"
        assert "stream-ui" not in visible
        assert "{" not in visible

    def test_a_block_becomes_start_items_end(self):
        _, events = drive([FENCED])
        assert kinds(events) == [EVENT_BLOCK_START, EVENT_ITEM, EVENT_ITEM, EVENT_BLOCK_END]
        assert payloads(events, EVENT_BLOCK_END)[0]["total"] == 2

    def test_the_header_json_becomes_the_blocks_props(self):
        _, events = drive([FENCED])
        start = payloads(events, EVENT_BLOCK_START)[0]
        assert start["schema"] == "movie-list"
        assert start["props"] == {"title": "Sci-fi"}

    def test_a_homogeneous_blocks_items_may_omit_their_schema(self):
        """Twenty films should not cost twenty repetitions of the item name."""
        _, events = drive([FENCED])
        items = payloads(events, EVENT_ITEM)
        assert [item["schema"] for item in items] == ["movie-card-item"] * 2
        assert [item["data"] for item in items] == [{"id": 603}, {"id": 27205}]

    def test_a_heterogeneous_blocks_items_carry_their_own_schema(self):
        text = '```stream-ui {"schema": "mixed"}\n{"schema": "note", "text": "hi"}\n```\n'
        _, events = drive([text])
        item = payloads(events, EVENT_ITEM)[0]
        assert item["schema"] == "note"
        assert item["data"] == {"text": "hi"}

    def test_a_heterogeneous_item_without_a_schema_is_an_error(self):
        text = '```stream-ui {"schema": "mixed"}\n{"text": "hi"}\n```\n'
        _, events = drive([text])
        assert "schema" in payloads(events, EVENT_ITEM)[0]["error"]

    def test_a_single_card_needs_no_list_wrapper(self):
        text = '```stream-ui {"schema": "weather"}\n{"city": "北京", "temp_c": 21, "condition": "rain"}\n```\n'
        _, events = drive([text])
        assert payloads(events, EVENT_ITEM)[0]["data"]["city"] == "北京"

    def test_a_malformed_line_does_not_kill_the_block(self):
        text = '```stream-ui {"schema": "movie-list"}\n{"id": 1}\nnot json\n{"id": 2}\n```\n'
        _, events = drive([text])
        items = payloads(events, EVENT_ITEM)
        assert [item.get("data") for item in items] == [{"id": 1}, None, {"id": 2}]
        assert "invalid JSON" in items[1]["error"]
        assert items[1]["raw"] == "not json"

    def test_ordinary_code_fences_are_untouched(self):
        text = "Run this:\n\n```python\nprint('hi')\n```\n"
        visible, events = drive([text])
        assert visible == text
        assert events == []

    def test_two_blocks_in_one_message_get_distinct_ids(self):
        text = (
            '```stream-ui {"schema": "movie-list"}\n{"id":1}\n```\nmiddle\n'
            '```stream-ui {"schema": "movie-list"}\n{"id":2}\n```\n'
        )
        visible, events = drive([text])
        assert visible == "middle\n"
        assert kinds(events).count(EVENT_BLOCK_START) == 2
        assert len({payload["blockId"] for _, payload in events}) == 2

    def test_blank_lines_inside_a_fence_are_ignored(self):
        text = '```stream-ui {"schema": "movie-list"}\n{"id":1}\n\n\n{"id":2}\n```\n'
        _, events = drive([text])
        assert kinds(events).count(EVENT_ITEM) == 2


class TestHeaderErrors:
    """A header the model got wrong becomes a visible block, not a silent loss."""

    def test_invalid_header_json_degrades_the_whole_block(self):
        _, events = drive(["```stream-ui {oops}\nbody\n```\n"])
        start = payloads(events, EVENT_BLOCK_START)[0]
        assert "invalid header JSON" in start["error"]
        assert start["raw"] == "```stream-ui {oops}"

    def test_a_degraded_block_keeps_its_body_verbatim(self):
        _, events = drive(['```stream-ui {oops}\n{"id": 1}\nplain\n```\n'])
        assert "".join(p["delta"] for p in payloads(events, EVENT_TEXT)) == '{"id": 1}\nplain\n'

    def test_a_missing_schema_key_is_an_error(self):
        _, events = drive(['```stream-ui {"title": "x"}\nbody\n```\n'])
        assert "schema" in payloads(events, EVENT_BLOCK_START)[0]["error"]

    def test_an_unregistered_schema_is_an_error_block(self):
        _, events = drive(['```stream-ui {"schema": "nope"}\nbody\n```\n'])
        assert payloads(events, EVENT_BLOCK_START)[0]["error"] == "unknown block schema 'nope'"

    def test_allow_unknown_passes_the_block_through(self):
        _, events = drive(
            ['```stream-ui {"schema": "nope"}\n{"a": 1}\n```\n'], catalog(allow_unknown=True)
        )
        assert "error" not in payloads(events, EVENT_BLOCK_START)[0]
        assert payloads(events, EVENT_ITEM)[0]["data"] == {"a": 1}


class TestTextBody:
    """Raw bodies exist so a code card can stream, and so nothing needs escaping."""

    def test_code_is_emitted_verbatim_without_escaping(self):
        source = 'def main():\n    path = "C:\\tmp"    # a \\ and a "\n'
        _, events = drive(
            [f'```stream-ui {{"schema": "code-card", "lang": "python"}}\n{source}```\n']
        )
        assert "".join(p["delta"] for p in payloads(events, EVENT_TEXT)) == source

    def test_the_body_is_never_parsed_as_json(self):
        _, events = drive(['```stream-ui {"schema": "code-card", "lang": "json"}\n{"a": 1}\n```\n'])
        assert payloads(events, EVENT_ITEM) == []
        assert payloads(events, EVENT_TEXT)[0]["delta"] == '{"a": 1}\n'

    def test_text_arrives_progressively_rather_than_all_at_the_end(self):
        """A hundred-line card must render as it is written, not thirty seconds later."""
        body = "".join(f"line {i}\n" for i in range(100))
        parser = StreamUIFenceParser(catalog())
        header = '```stream-ui {"schema": "code-card", "lang": "text"}\n'
        parser.feed(header)
        seen = 0
        for char in body:
            _, produced = parser.feed(char)
            seen += sum(1 for e in produced if e.kind == EVENT_TEXT)
        assert seen > 50

    def test_a_longer_fence_survives_a_body_containing_backticks(self):
        text = (
            '````stream-ui {"schema": "code-card", "lang": "md"}\n```py\nx = 1\n```\n````\nafter\n'
        )
        visible, events = drive([text])
        assert visible == "after\n"
        assert "".join(p["delta"] for p in payloads(events, EVENT_TEXT)) == "```py\nx = 1\n```\n"

    def test_a_short_fence_still_closes_on_three_backticks(self):
        _, events = drive(['```stream-ui {"schema": "code-card", "lang": "py"}\nx\n```\nafter\n'])
        assert payloads(events, EVENT_BLOCK_END)[0]["truncated"] is False

    def test_xml_scheme_a_multiline_items(self):
        text = (
            "Intro\n"
            "<stream-ui>\n"
            '{"schema": "movie-list", "title": "Sci-fi"}\n'
            '{"id": 603}\n'
            '{"id": 27205}\n'
            "</stream-ui>\n"
            "Outro\n"
        )
        visible, events = drive([text])
        assert visible == "Intro\nOutro\n"
        starts = payloads(events, EVENT_BLOCK_START)
        assert len(starts) == 1
        assert starts[0]["schema"] == "movie-list"
        assert starts[0]["props"] == {"title": "Sci-fi"}
        items = payloads(events, EVENT_ITEM)
        assert len(items) == 2
        assert items[0]["data"] == {"id": 603}
        assert items[1]["data"] == {"id": 27205}
        ends = payloads(events, EVENT_BLOCK_END)
        assert len(ends) == 1
        assert ends[0]["truncated"] is False

    def test_xml_nested_code_blocks_survive_without_truncation(self):
        code_content = (
            "# My Architecture Report\n\n"
            "Here is Python code with standard 3 backticks:\n"
            "```python\n"
            "def handle_event(event):\n"
            "    print(f'Received: {event}')\n"
            "    return True\n"
            "```\n\n"
            "And here is bash code:\n"
            "```bash\n"
            "curl -X POST http://localhost:8000/api\n"
            "```\n\n"
            "Even 4 backticks or unmatched backticks:\n"
            "````nested\n"
            "test\n"
            "````\n"
        )
        text = (
            "Start report:\n"
            "<stream-ui>\n"
            '{"schema": "code-card", "lang": "md"}\n'
            f"{code_content}"
            "</stream-ui>\n"
            "Finished successfully.\n"
        )
        visible, events = drive([text])
        assert visible == "Start report:\nFinished successfully.\n"
        starts = payloads(events, EVENT_BLOCK_START)
        assert len(starts) == 1
        assert starts[0]["schema"] == "code-card"
        assert starts[0]["body"] == "text"
        full_text = "".join(p["delta"] for p in payloads(events, EVENT_TEXT))
        assert full_text == code_content
        ends = payloads(events, EVENT_BLOCK_END)
        assert len(ends) == 1
        assert ends[0]["truncated"] is False

    def test_xml_inline_json_header(self):
        text = (
            '<stream-ui {"schema": "code-card", "lang": "py"}>\n'
            "print('inline json')\n"
            "</stream-ui>\n"
        )
        visible, events = drive([text])
        assert visible == ""
        starts = payloads(events, EVENT_BLOCK_START)
        assert len(starts) == 1
        assert starts[0]["schema"] == "code-card"
        assert starts[0]["props"] == {"lang": "py"}
        full_text = "".join(p["delta"] for p in payloads(events, EVENT_TEXT))
        assert full_text == "print('inline json')\n"

    def test_xml_attributes_header(self):
        text = '<stream-ui schema="code-card" lang="python">\nx = 42\n</stream-ui>\n'
        visible, events = drive([text])
        assert visible == ""
        starts = payloads(events, EVENT_BLOCK_START)
        assert len(starts) == 1
        assert starts[0]["schema"] == "code-card"
        assert starts[0]["props"] == {"lang": "python"}
        full_text = "".join(p["delta"] for p in payloads(events, EVENT_TEXT))
        assert full_text == "x = 42\n"

    def test_xml_immediate_close_without_header(self):
        text = "<stream-ui>\n</stream-ui>\n"
        visible, events = drive([text])
        assert visible == ""
        starts = payloads(events, EVENT_BLOCK_START)
        assert len(starts) == 1
        assert starts[0]["error"] is not None
        ends = payloads(events, EVENT_BLOCK_END)
        assert len(ends) == 1

    def test_xml_multiline_json_header(self):
        text = (
            "<stream-ui>\n"
            "{\n"
            '  "schema": "code-card",\n'
            '  "lang": "python"\n'
            "}\n"
            "print('multiline json header')\n"
            "</stream-ui>\n"
        )
        visible, events = drive([text])
        assert visible == ""
        starts = payloads(events, EVENT_BLOCK_START)
        assert len(starts) == 1
        assert starts[0]["schema"] == "code-card"
        assert starts[0]["props"] == {"lang": "python"}
        full_text = "".join(p["delta"] for p in payloads(events, EVENT_TEXT))
        assert full_text == "print('multiline json header')\n"


class TestStreaming:
    """The fence must survive being chopped anywhere, including mid-marker."""

    @pytest.mark.parametrize(
        "source",
        [
            FENCED,
            '```stream-ui {"schema": "code-card", "lang": "py"}\nx = 1\ny = 2\n```\ntail\n',
            '````stream-ui {"schema": "code-card", "lang": "md"}\n```\n````\n',
            '<stream-ui>\n{"schema": "code-card", "lang": "py"}\nx = 1\ny = 2\n</stream-ui>\ntail\n',
            '<stream-ui>\n{"schema": "code-card", "lang": "md"}\n```python\nx = 1\n```\n</stream-ui>\ntail\n',
        ],
    )
    def test_every_two_way_split_gives_the_same_result(self, source):
        expected_text, expected_events = drive([source])
        expected = normalize(expected_events)
        for cut in range(1, len(source)):
            visible, events = drive([source[:cut], source[cut:]])
            assert visible == expected_text, f"text differs when split at {cut}"
            assert normalize(_merge_text(events)) == normalize(_merge_text(expected_events)), (
                f"events differ when split at {cut}"
            )
            assert len(normalize(events)) >= len(expected) - len(source)

    def test_character_by_character_gives_the_same_result(self):
        expected_text, expected_events = drive([FENCED])
        visible, events = drive(list(FENCED))
        assert visible == expected_text
        assert normalize(events) == normalize(expected_events)

    def test_xml_character_by_character_gives_the_same_result(self):
        source = (
            "<stream-ui>\n"
            '{"schema": "code-card", "lang": "md"}\n'
            "# Title\n"
            "```python\n"
            "x = 1\n"
            "```\n"
            "</stream-ui>\n"
            "tail\n"
        )
        expected_text, expected_events = drive([source])
        visible, events = drive(list(source))
        assert visible == expected_text
        assert normalize(_merge_text(events)) == normalize(_merge_text(expected_events))

    def test_xml_partial_marker_is_held_back(self):
        parser = StreamUIFenceParser(catalog())
        visible, events = parser.feed("<str")
        assert visible == ""
        assert events == []
        visible, events = parser.feed("eam-ui>\n")
        assert visible == ""
        assert events == []
        visible, events = parser.feed('{"schema": "movie-list"}\n')
        assert visible == ""
        assert [e.kind for e in events] == [EVENT_BLOCK_START]

    @pytest.mark.parametrize("seed", range(5))
    def test_random_multi_way_splits_give_the_same_result(self, seed):
        rng = random.Random(seed)
        expected_text, expected_events = drive([FENCED])
        cuts = sorted(rng.sample(range(1, len(FENCED)), 4))
        chunks = [FENCED[a:b] for a, b in zip([0, *cuts], [*cuts, len(FENCED)], strict=True)]
        visible, events = drive(chunks)
        assert visible == expected_text
        assert normalize(events) == normalize(expected_events)

    def test_a_partial_marker_is_held_back_not_leaked(self):
        parser = StreamUIFenceParser(catalog())
        visible, events = parser.feed("``")
        assert visible == ""
        assert events == []
        visible, events = parser.feed('`stream-ui {"schema": "movie-list"}\n')
        assert visible == ""
        assert [e.kind for e in events] == [EVENT_BLOCK_START]

    def test_a_held_back_marker_is_released_once_it_cannot_be_a_fence(self):
        visible, events = drive(["``", "` plain\n"])
        assert visible == "``` plain\n"
        assert events == []

    def test_backticks_mid_line_are_not_a_fence(self):
        visible, events = drive(["use ``` for code\n"])
        assert visible == "use ``` for code\n"
        assert events == []


def _merge_text(events):
    """Collapse consecutive ui.text events, whose split depends on chunking."""
    out: list[tuple[str, dict]] = []
    for kind, payload in events:
        if kind == EVENT_TEXT and out and out[-1][0] == EVENT_TEXT:
            merged = dict(out[-1][1])
            merged["delta"] += payload["delta"]
            out[-1] = (kind, merged)
        else:
            out.append((kind, dict(payload)))
    return out


class TestTruncation:
    def test_an_unclosed_fence_is_closed_and_marked_truncated(self):
        visible, events = drive(['before\n```stream-ui {"schema": "movie-list"}\n{"id":1}\n'])
        assert visible == "before\n"
        assert payloads(events, EVENT_BLOCK_END)[0]["truncated"] is True

    def test_a_trailing_partial_line_is_still_parsed(self):
        _, events = drive(['```stream-ui {"schema": "movie-list"}\n{"id":1}'])
        assert payloads(events, EVENT_ITEM)[0]["data"] == {"id": 1}

    def test_a_dangling_opening_marker_does_not_leak(self):
        visible, events = drive(["answer\n```stream-ui"])
        assert visible == "answer\n"
        assert payloads(events, EVENT_BLOCK_END)[0]["truncated"] is True

    def test_dangling_backticks_that_are_not_ours_are_shown(self):
        visible, events = drive(["answer\n```"])
        assert visible == "answer\n```"
        assert events == []


class TestOneShotParse:
    def test_the_history_parse_matches_the_streaming_one(self):
        streamed_text, streamed_events = drive(list(FENCED))
        text, blocks = parse_streamui_text(FENCED, catalog())
        assert text == streamed_text
        assert len(blocks) == 1
        assert [item.data for item in blocks[0].items] == [
            payload["data"] for payload in payloads(streamed_events, EVENT_ITEM)
        ]

    def test_text_without_a_fence_is_returned_unchanged(self):
        text, blocks = parse_streamui_text("just an answer", catalog())
        assert text == "just an answer"
        assert blocks == []


class TestCatalogValidation:
    def test_a_closed_vocabulary_rejects_a_word_outside_it(self):
        """The frontend maps these tokens to assets; an unknown one has no asset."""
        _, error = catalog().validate_item(
            "weather", {"city": "BJ", "temp_c": 1, "condition": "hail"}
        )
        assert "condition" in error

    def test_a_media_url_must_be_https(self):
        _, error = catalog().validate_item(
            "weather",
            {"city": "BJ", "temp_c": 1, "condition": "rain", "icon": "http://x/a.png"},
        )
        assert "https" in error

    def test_a_media_url_must_be_on_an_allowed_host(self):
        cat = catalog(allowed_media_domains=["cdn.example.com"])
        _, error = cat.validate_item(
            "weather",
            {"city": "BJ", "temp_c": 1, "condition": "rain", "icon": "https://evil.com/a.png"},
        )
        assert "allowed media domains" in error
        _, ok = cat.validate_item(
            "weather",
            {
                "city": "BJ",
                "temp_c": 1,
                "condition": "rain",
                "icon": "https://img.cdn.example.com/a.png",
            },
        )
        assert ok is None

    def test_an_unexpected_field_is_rejected(self):
        _, error = catalog().validate_props("movie-list", {"titel": "typo"})
        assert "titel" in error

    def test_merging_catalogs_detects_a_name_collision(self):
        class OtherMovieList(BlockSchema):
            schema_name = "movie-list"

        with pytest.raises(CatalogConflict, match="movie-list"):
            _ = catalog() | CardCatalog([OtherMovieList])

    def test_merging_catalogs_keeps_both_sets_of_schemas(self):
        class Chart(BlockSchema):
            schema_name = "chart"

        merged = catalog() | CardCatalog([Chart])
        assert "chart" in merged.block_names
        assert "movie-list" in merged.block_names


class TestPrompt:
    def test_the_prompt_describes_every_registered_schema(self):
        prompt = catalog().to_prompt()
        for name in ("movie-list", "code-card", "weather"):
            assert f"### {name}" in prompt

    def test_the_prompt_lists_a_closed_vocabulary_verbatim(self):
        prompt = catalog().to_prompt()
        assert '"clear"' in prompt and '"storm"' in prompt

    def test_the_prompt_says_which_body_a_schema_uses(self):
        prompt = catalog().to_prompt()
        assert "raw text" in prompt
        assert "one JSON object per line" in prompt

    def test_the_prompt_steers_plain_code_away_from_cards(self):
        """Otherwise the model wraps every snippet it writes in a card."""
        assert "```language fence" in catalog().to_prompt()

    def test_an_empty_catalog_contributes_nothing(self):
        assert CardCatalog().to_prompt() == ""

    def test_prompt_filtering_with_include_and_exclude(self):
        cat = catalog()
        all_prompt = cat.to_prompt()
        assert "### movie-list" in all_prompt
        assert "### weather" in all_prompt

        # Test include
        only_weather = cat.to_prompt(include=["weather"])
        assert "### weather" in only_weather
        assert "### movie-list" not in only_weather
        assert "### code-card" not in only_weather

        # Test exclude
        no_weather = cat.to_prompt(exclude=["weather"])
        assert "### weather" not in no_weather
        assert "### movie-list" in no_weather
        assert "### code-card" in no_weather

    def test_prompt_filtering_with_include_in_system_prompt_flag(self):
        class HiddenToolCard(BlockSchema):
            schema_name = "tool-diff"
            include_in_system_prompt = False

        cat = catalog()
        cat.register(HiddenToolCard)
        assert cat.knows_block("tool-diff")

        # Default system prompt skips tool-diff
        default_prompt = cat.to_prompt()
        assert "### tool-diff" not in default_prompt
        assert "### weather" in default_prompt

        # Explicitly asking for it via include bypasses system_prompt_only
        explicit_prompt = cat.to_prompt(include=["tool-diff"])
        assert "### tool-diff" in explicit_prompt

    def test_the_json_schema_covers_blocks_and_items(self):
        schema = catalog().to_json_schema()
        assert "movie-list" in schema["blocks"]
        assert schema["blocks"]["code-card"]["body"] == "text"
        assert "movie-card-item" in schema["items"]
