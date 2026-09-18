"""Tests for StreamingArtifactToolkit, ArtifactCard, and PresentationDeck."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agno_harness import (
    AgentRuntime,
    ArtifactCard,
    PresentationDeck,
    SequencerMode,
    SlideProgressItem,
    StreamingArtifactToolkit,
    append_artifact,
    patch_artifact,
    read_artifact_section,
    stream_artifact,
)
from agno_harness.core.streamui import CardCatalog

from .conftest import FakeAgent, collect, content, customs, make_input, run_completed


def test_artifact_card_schema():
    assert ArtifactCard.schema_name == "artifact"
    assert ArtifactCard.body == "text"
    assert ArtifactCard.emit_text is True

    card = ArtifactCard(title="Test Doc", path="docs/readme.md", summary="Brief summary")
    assert card.title == "Test Doc"
    assert card.path == "docs/readme.md"
    assert card.summary == "Brief summary"


def test_presentation_deck_schema_and_parse_line():
    assert PresentationDeck.schema_name == "presentation_deck"
    assert PresentationDeck.body == "text"
    assert PresentationDeck.emit_text is False
    assert PresentationDeck.item is SlideProgressItem

    # Various slide comment formats
    p1 = PresentationDeck.parse_line("<!-- SLIDE: 1 -->", None)
    assert p1 == {"page": 1, "title": None}

    p2 = PresentationDeck.parse_line("<!-- SLIDE: 2 - System Architecture -->", None)
    assert p2 == {"page": 2, "title": "System Architecture"}

    p3 = PresentationDeck.parse_line("<!-- PAGE: 3: Deep Dive -->", None)
    assert p3 == {"page": 3, "title": "Deep Dive"}

    p4 = PresentationDeck.parse_line("<!-- SLIDES PAGE: 4 - Conclusion -->", None)
    assert p4 == {"page": 4, "title": "Conclusion"}

    # Non-slide comments or lines return None
    assert PresentationDeck.parse_line("<div>Some content</div>", None) is None
    assert PresentationDeck.parse_line("<!-- normal comment -->", None) is None


def test_streaming_artifact_toolkit_instructions():
    toolkit = StreamingArtifactToolkit()
    instructions = (
        toolkit.instructions() if callable(toolkit.instructions) else str(toolkit.instructions)
    )
    assert "artifact" in instructions
    assert "presentation_deck" in instructions
    assert "NEVER call traditional file write or edit tools" in instructions


@pytest.mark.asyncio
async def test_stream_artifact_tool(tmp_path):
    catalog = CardCatalog([ArtifactCard])
    runtime = AgentRuntime(
        agent=FakeAgent([]),
        catalog=catalog,
        sequencer_mode=SequencerMode.AUDIT,
        artifact_root_dir=str(tmp_path / "{task-id}"),
    )

    async def run_tool(**kwargs):
        res = await stream_artifact(
            title="My Report",
            content="# Heading\nParagraph content here.",
            path="reports/test.md",
            summary="A test report",
        )
        assert "streamed to UI" in res
        yield run_completed()

    runtime.agent.arun = lambda **kwargs: run_tool(**kwargs)
    events = await collect(runtime.stream_events(make_input()))

    starts = customs(events, "ui.block.start")
    assert len(starts) == 1
    assert starts[0].value["schema"] == "artifact"
    assert starts[0].value["props"]["title"] == "My Report"

    texts = customs(events, "ui.text")
    combined = "".join(t.value["delta"] for t in texts)
    assert "# Heading\nParagraph content here." in combined

    ends = customs(events, "ui.block.end")
    assert len(ends) == 1
    assert ends[0].value["savedPath"] is not None
    assert ends[0].value["relativePath"] == "reports/test.md"


@pytest.mark.asyncio
async def test_presentation_deck_streaming_and_persistence(tmp_path):
    catalog = CardCatalog([PresentationDeck])
    root_dir = str(tmp_path / "workspaces" / "{task-id}")
    runtime = AgentRuntime(
        agent=FakeAgent(
            [
                content(
                    '```stream-ui {"schema": "presentation_deck", "title": "AI Agent Architecture", "filepath": "dist/slides.html", "total_slides": 3}\n'
                ),
                content("<!DOCTYPE html><html><head><title>Slides</title></head><body>\n"),
                content("<!-- SLIDE: 1 - Welcome -->\n<section><h1>Slide 1</h1></section>\n"),
                content("<!-- SLIDE: 2 - Core Pipeline -->\n<section><h1>Slide 2</h1></section>\n"),
                content("<!-- SLIDE: 3 - Final Wrap -->\n<section><h1>Slide 3</h1></section>\n"),
                content("</body></html>\n"),
                content("```\n"),
                run_completed(),
            ]
        ),
        catalog=catalog,
        artifact_root_dir=root_dir,
    )

    inp = make_input()
    events = await collect(runtime.stream_events(inp))

    # emit_text=False: raw HTML text is NOT emitted as ui.text events
    text_events = customs(events, "ui.text")
    assert len(text_events) == 0

    # Incremental slide progress items ARE emitted
    slide_items = customs(events, "ui.item")
    assert len(slide_items) == 3
    assert slide_items[0].value["data"] == {"page": 1, "title": "Welcome"}
    assert slide_items[1].value["data"] == {"page": 2, "title": "Core Pipeline"}
    assert slide_items[2].value["data"] == {"page": 3, "title": "Final Wrap"}

    # ui.block.end contains persistence metadata
    end_events = customs(events, "ui.block.end")
    assert len(end_events) == 1
    end_val = end_events[0].value
    assert end_val["relativePath"] == "dist/slides.html"
    expected_output = tmp_path / "workspaces" / inp.thread_id / "dist" / "slides.html"
    assert end_val["savedPath"] == str(expected_output)
    assert expected_output.exists()
    content_on_disk = expected_output.read_text(encoding="utf-8")
    assert "<title>Slides</title>" in content_on_disk
    assert "<!-- SLIDE: 2 - Core Pipeline -->" in content_on_disk


@pytest.mark.asyncio
async def test_artifact_card_with_nested_code_blocks_and_xml_streaming(tmp_path):
    catalog = CardCatalog([ArtifactCard])
    root_dir = str(tmp_path / "artifacts" / "{task-id}")
    runtime = AgentRuntime(
        agent=FakeAgent(
            [
                content("<stream-ui>\n"),
                content(
                    '{"schema": "artifact", "title": "Architecture Report", "path": "reports/arch.md"}\n'
                ),
                content("# Architecture Report\n\n"),
                content("```python\n"),
                content("def process_event(event):\n    return {'status': 'ok'}\n"),
                content("```\n\n"),
                content("Next steps:\n```bash\nmake run\n```\n"),
                content("</stream-ui>\n"),
                content("Report generation completed.\n"),
                run_completed(),
            ]
        ),
        catalog=catalog,
        artifact_root_dir=root_dir,
    )

    inp = make_input()
    events = await collect(runtime.stream_events(inp))

    # ui.block.start must have schema "artifact" and body "text"
    starts = customs(events, "ui.block.start")
    assert len(starts) == 1
    assert starts[0].value["schema"] == "artifact"
    assert starts[0].value["body"] == "text"
    assert starts[0].value["props"]["title"] == "Architecture Report"

    # ui.text deltas must contain the code blocks without cutting off
    text_events = customs(events, "ui.text")
    assert len(text_events) > 0
    full_text = "".join(e.value["delta"] for e in text_events)
    assert "# Architecture Report" in full_text
    assert "```python\ndef process_event(event):\n    return {'status': 'ok'}\n```" in full_text
    assert "```bash\nmake run\n```" in full_text

    # ui.block.end occurs only at </stream-ui>
    ends = customs(events, "ui.block.end")
    assert len(ends) == 1
    assert ends[0].value["relativePath"] == "reports/arch.md"

    # File persisted to disk with entire markdown and code blocks
    saved_file = tmp_path / "artifacts" / inp.thread_id / "reports" / "arch.md"
    assert saved_file.exists()
    disk_content = saved_file.read_text(encoding="utf-8")
    assert "def process_event(event):" in disk_content
    assert "make run" in disk_content


@pytest.mark.asyncio
async def test_presentation_deck_streaming_token_chunks_extracts_all_items(tmp_path: Path):
    """Token-by-token streaming must not slice lines and miss slide extractions."""
    from agno_harness.core.streamui import CardCatalog, StreamUIFenceParser

    cat = CardCatalog([PresentationDeck])

    source = (
        "<stream-ui>\n"
        "{\n"
        '  "schema": "presentation_deck",\n'
        '  "title": "Token Deck",\n'
        '  "filepath": "output/deck.html",\n'
        '  "total_slides": 3\n'
        "}\n"
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <!-- SLIDE: 1 - Introduction -->\n"
        "  <div class='slide'>Slide 1</div>\n"
        "  <!-- SLIDE: 2 - Core Architecture -->\n"
        "  <div class='slide'>Slide 2</div>\n"
        "  <!-- SLIDE: 3 - Conclusion -->\n"
        "  <div class='slide'>Slide 3</div>\n"
        "</html>\n"
        "</stream-ui>\n"
    )

    for chunk_size in (1, 3, 7, 20):
        parser = StreamUIFenceParser(cat)
        events = []
        for i in range(0, len(source), chunk_size):
            _, ev = parser.feed(source[i : i + chunk_size])
            events.extend(ev)
        _, ev = parser.flush()
        events.extend(ev)

        items = [e for e in events if e.kind == "ui.item"]
        assert len(items) == 3, f"Failed for chunk_size={chunk_size}: got {len(items)} items"
        assert items[0].payload["data"]["page"] == 1
        assert items[0].payload["data"]["title"] == "Introduction"
        assert items[1].payload["data"]["page"] == 2
        assert items[1].payload["data"]["title"] == "Core Architecture"
        assert items[2].payload["data"]["page"] == 3
        assert items[2].payload["data"]["title"] == "Conclusion"


def test_read_artifact_section(tmp_path: Path):
    doc_dir = tmp_path / "data" / "artifacts" / "default"
    doc_dir.mkdir(parents=True)
    file_path = doc_dir / "sample.html"
    content = "\n".join(f"line {i} - some code" for i in range(1, 101))
    file_path.write_text(content, encoding="utf-8")

    # Read section with start_line and line_count
    res_raw = read_artifact_section(
        "sample.html", start_line=10, line_count=5, configured_root=doc_dir
    )
    res = json.loads(res_raw)
    assert res["status"] == "success"
    assert res["start_line"] == 10
    assert res["end_line"] == 14
    assert res["total_lines"] == 100
    assert res["has_more"] is True
    assert "10 | line 10" in res["content"]
    assert "14 | line 14" in res["content"]

    # Query search
    res_query = json.loads(
        read_artifact_section("sample.html", query="line 42", line_count=6, configured_root=doc_dir)
    )
    assert res_query["status"] == "success"
    assert res_query["match_count"] == 1
    assert 42 in res_query["matched_lines"]
    assert "42 | line 42" in res_query["content"]

    # File not found
    err_res = json.loads(read_artifact_section("nonexistent.txt", configured_root=doc_dir))
    assert err_res["status"] == "error"
    assert "not found" in err_res["error"]

    # Path traversal protection
    sec_res = json.loads(read_artifact_section("../../etc/passwd", configured_root=doc_dir))
    assert sec_res["status"] == "error"
    assert "Security error" in sec_res["error"]


@pytest.mark.asyncio
async def test_patch_artifact(tmp_path: Path):
    doc_dir = tmp_path / "artifacts"
    doc_dir.mkdir(parents=True)
    file_path = doc_dir / "deck.html"
    file_path.write_text(
        "<html>\n<!-- SLIDE: 1 - Intro -->\n<div class='slide'>Hello</div>\n</html>\n",
        encoding="utf-8",
    )

    # Patch slide title and body
    search = "<!-- SLIDE: 1 - Intro -->\n<div class='slide'>Hello</div>"
    replace = "<!-- SLIDE: 1 - Introduction -->\n<div class='slide'>Hello World!</div>"
    patch_raw = await patch_artifact("deck.html", search, replace, configured_root=doc_dir)
    res = json.loads(patch_raw)

    assert res["status"] == "success"
    assert res["action"] == "patch"
    assert res["replaced_lines_span"] == [2, 3]
    assert res["lines_added"] == 2
    assert res["lines_removed"] == 2

    new_file_text = file_path.read_text(encoding="utf-8")
    assert "Hello World!" in new_file_text
    assert "Introduction" in new_file_text

    # Search block not found
    err_raw = await patch_artifact(
        "deck.html", "Non-existent content", "new", configured_root=doc_dir
    )
    err = json.loads(err_raw)
    assert err["status"] == "error"
    assert "search_block was not found" in err["error"]


@pytest.mark.asyncio
async def test_append_artifact(tmp_path: Path):
    doc_dir = tmp_path / "artifacts"
    doc_dir.mkdir(parents=True)
    file_path = doc_dir / "deck.html"
    file_path.write_text("<!DOCTYPE html>\n<html>\n<!-- SLIDE: 1 -->\n", encoding="utf-8")

    append_part = "<!-- SLIDE: 2 -->\n<div>Slide 2</div>\n</html>\n"
    res_raw = await append_artifact("deck.html", append_part, configured_root=doc_dir)
    res = json.loads(res_raw)

    assert res["status"] == "success"
    assert res["action"] == "append"
    assert res["is_complete_html"] is True
    assert res["slide_count"] == 2

    full_text = file_path.read_text(encoding="utf-8")
    assert "<!-- SLIDE: 2 -->" in full_text
    assert "</html>" in full_text


@pytest.mark.asyncio
async def test_streamui_mode_append_persistence(tmp_path: Path):
    catalog = CardCatalog([PresentationDeck])
    art_dir = tmp_path / "{task-id}"
    runtime = AgentRuntime(
        agent=FakeAgent([]),
        catalog=catalog,
        sequencer_mode=SequencerMode.AUDIT,
        artifact_root_dir=str(art_dir),
    )

    # First turn: stream initial slides
    part1 = (
        "<stream-ui>\n"
        '{"schema": "presentation_deck", "title": "Deck", "filepath": "deck.html", "total_slides": 4}\n'
        "<html>\n<!-- SLIDE: 1 -->\n<div>Page 1</div>\n<!-- SLIDE: 2 -->\n<div>Page 2</div>\n"
        "</stream-ui>\n"
    )
    inp1 = make_input("Create deck", thread_id="t1")
    agent1 = FakeAgent([content(part1), run_completed()])
    runtime.agent = agent1
    runtime.runner.agent = agent1
    await collect(runtime.stream_events(inp1))

    saved_file = tmp_path / "t1" / "deck.html"
    assert saved_file.exists()
    assert "<!-- SLIDE: 1 -->" in saved_file.read_text()
    assert "<!-- SLIDE: 3 -->" not in saved_file.read_text()

    # Second turn: append remaining slides with mode="append"
    part2 = (
        "<stream-ui>\n"
        '{"schema": "presentation_deck", "title": "Deck", "filepath": "deck.html", "mode": "append"}\n'
        "<!-- SLIDE: 3 -->\n<div>Page 3</div>\n<!-- SLIDE: 4 -->\n<div>Page 4</div>\n</html>\n"
        "</stream-ui>\n"
    )
    inp2 = make_input("Continue deck", thread_id="t1")
    agent2 = FakeAgent([content(part2), run_completed()])
    runtime.agent = agent2
    runtime.runner.agent = agent2
    await collect(runtime.stream_events(inp2))

    full_text = saved_file.read_text()
    assert "<!-- SLIDE: 1 -->" in full_text
    assert "<!-- SLIDE: 3 -->" in full_text
    assert "<!-- SLIDE: 4 -->" in full_text
    assert "</html>" in full_text
