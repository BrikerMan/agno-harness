"""Keyword search over a directory of Markdown files.

Each search reads the files again, so an edit is visible on the next turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_MAX_FILE_BYTES = 512_000


class MarkdownKnowledge:
    """Live memory backed by Markdown files on disk."""

    def __init__(self, root: str | Path, *, max_chars: int = 4000) -> None:
        self.root = Path(root)
        self.max_chars = max_chars

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

        terms = [part for part in phrase.split() if part]
        ranked: list[tuple[int, str, str]] = []
        root = self.root.resolve()
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _MARKDOWN_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            score = _score(text, phrase, terms)
            if score <= 0:
                continue
            ranked.append(
                (score, relative.as_posix(), _excerpt(text, phrase, terms, self.max_chars))
            )

        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [{"path": path, "content": content} for _, path, content in ranked[:limit]]


def _score(text: str, phrase: str, terms: list[str]) -> int:
    lowered = text.lower()
    score = lowered.count(phrase.lower()) * 5
    for term in terms:
        score += lowered.count(term.lower())
    return score


def _excerpt(text: str, phrase: str, terms: list[str], max_chars: int) -> str:
    lowered = text.lower()
    at = lowered.find(phrase.lower())
    if at < 0:
        for term in terms:
            at = lowered.find(term.lower())
            if at >= 0:
                break
    if at < 0:
        at = 0
    start = max(0, at - max_chars // 4)
    chunk = text[start : start + max_chars].strip()
    if start > 0:
        chunk = "…" + chunk
    if start + max_chars < len(text):
        chunk = chunk + "…"
    return chunk
