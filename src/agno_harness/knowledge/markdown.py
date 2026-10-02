"""Keyword search over a directory of Markdown files.

Each search reads the files again, so an edit is visible on the next turn.
Ranking is the same bigram BM25 as :class:`KeywordKnowledge`, so Chinese notes
match without spaces between words.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .keyword import CjkKeywordScorer

_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_MAX_FILE_BYTES = 512_000


class MarkdownKnowledge:
    """Live memory backed by Markdown files on disk."""

    def __init__(self, root: str | Path, *, max_chars: int = 4000) -> None:
        self.root = Path(root)
        self.max_chars = max_chars
        self.scorer = CjkKeywordScorer({"path": 1.0, "content": 1.0})

    def _notes(self) -> list[dict[str, str]]:
        root = self.root.resolve()
        notes: list[dict[str, str]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _MARKDOWN_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            notes.append(
                {
                    "path": relative.as_posix(),
                    "content": path.read_text(encoding="utf-8", errors="replace"),
                }
            )
        return notes

    def search(
        self,
        query: str,
        num_documents: int | None = 5,
        **_: Any,
    ) -> list[dict[str, str]]:
        """Return the best-matching notes. Suitable as an Agno ``knowledge_retriever``."""
        limit = 5 if num_documents is None else num_documents
        phrase = query.strip()
        if limit <= 0 or not phrase or not self.root.is_dir():
            return []
        hits = self.scorer.rank(self._notes(), phrase, top_k=limit, tie_key="path")
        return [
            {
                "path": str(hit.doc["path"]),
                "content": _excerpt(
                    str(hit.doc["content"]), [phrase, *hit.matched_terms], self.max_chars
                ),
            }
            for hit in hits
        ]


def _excerpt(text: str, needles: list[str], max_chars: int) -> str:
    lowered = text.lower()
    at = next((i for i in (lowered.find(n.lower()) for n in needles if n) if i >= 0), 0)
    start = max(0, at - max_chars // 4)
    chunk = text[start : start + max_chars].strip()
    if start > 0:
        chunk = "…" + chunk
    if start + max_chars < len(text):
        chunk = chunk + "…"
    return chunk
