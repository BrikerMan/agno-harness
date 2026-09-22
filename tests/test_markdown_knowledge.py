"""Markdown notes: edit a file, the next search sees it."""

from agno_harness import MarkdownKnowledge


def test_search_reads_markdown_and_picks_up_edits(tmp_path):
    notes = tmp_path / "knowledge"
    notes.mkdir()
    (notes / "memory.md").write_text(
        "# Memory\n\nThe office wifi password is river-lantern.\n", encoding="utf-8"
    )
    (notes / "other.md").write_text("# Other\n\nNothing about access here.\n", encoding="utf-8")

    library = MarkdownKnowledge(notes)
    hits = library.search("wifi password")
    assert [hit["path"] for hit in hits] == ["memory.md"]
    assert "river-lantern" in hits[0]["content"]

    (notes / "memory.md").write_text(
        "# Memory\n\nThe office wifi password is paper-crane.\n", encoding="utf-8"
    )
    again = library.search("wifi password")
    assert "paper-crane" in again[0]["content"]
    assert "river-lantern" not in again[0]["content"]


def test_missing_directory_and_blank_query_return_nothing(tmp_path):
    library = MarkdownKnowledge(tmp_path / "missing")
    assert library.search("anything") == []
    assert library.search("   ") == []
