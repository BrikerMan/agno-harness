"""Chinese keyword search without embeddings.

Text is cut into character bigrams (Latin words and digits stay whole) and
ranked with BM25, with a weight per field. Good enough for a few hundred
short documents, and every hit can say which words matched.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[a-z0-9]+")
_LATIN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Token:
    term: str
    start: int
    end: int


def tokenize(text: str) -> list[Token]:
    """Character bigrams for CJK runs, whole words for Latin and digits.

    Offsets point into ``text.lower()``, which has the same length as ``text``
    for the scripts matched here.
    """
    out: list[Token] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        seg, base = match.group(0), match.start()
        if _LATIN_RE.fullmatch(seg) or len(seg) == 1:
            out.append(Token(seg, base, base + len(seg)))
            continue
        out.extend(Token(seg[i : i + 2], base + i, base + i + 2) for i in range(len(seg) - 1))
    return out


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Iterable):
        return " ".join(str(v) for v in value if v)
    return str(value)


def merge_matched(query: str, tokens: Sequence[Token], hit: set[str]) -> list[str]:
    """Join adjacent matched bigrams back into words: 管架/架立/立柱 → 管架立柱."""
    spans: list[list[int]] = []
    for tok in tokens:
        if tok.term not in hit:
            continue
        if spans and tok.start < spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], tok.end)
        else:
            spans.append([tok.start, tok.end])
    lower = query.lower()
    return list(dict.fromkeys(lower[s:e] for s, e in spans))


@dataclass
class _Indexed:
    doc: Mapping[str, Any]
    tf: dict[str, float]
    length: float


@dataclass(frozen=True)
class ScoredDoc:
    doc: Mapping[str, Any]
    score: float
    matched_terms: list[str] = field(default_factory=list)


class CjkKeywordScorer:
    """BM25 over weighted fields. ``fields`` maps a document key to its weight."""

    def __init__(self, fields: Mapping[str, float], *, k1: float = 1.2, b: float = 0.75) -> None:
        if not fields:
            raise ValueError("fields must name at least one document key")
        self.fields = dict(fields)
        self.k1 = k1
        self.b = b

    def _index(self, doc: Mapping[str, Any]) -> _Indexed:
        tf: dict[str, float] = {}
        length = 0.0
        for key, weight in self.fields.items():
            for tok in tokenize(_field_text(doc.get(key))):
                tf[tok.term] = tf.get(tok.term, 0.0) + weight
                length += weight
        return _Indexed(doc, tf, length)

    def rank(
        self,
        docs: Iterable[Mapping[str, Any]],
        query: str,
        *,
        top_k: int = 5,
        tie_key: str | None = None,
    ) -> list[ScoredDoc]:
        tokens = tokenize(query)
        if not tokens or top_k <= 0:
            return []
        indexed = [self._index(d) for d in docs]
        if not indexed:
            return []
        avg_len = sum(d.length for d in indexed) / len(indexed) or 1.0
        terms = list(dict.fromkeys(t.term for t in tokens))
        df = {term: sum(1 for d in indexed if term in d.tf) for term in terms}
        n_docs = len(indexed)

        scored: list[ScoredDoc] = []
        for d in indexed:
            score = 0.0
            hit: set[str] = set()
            for term in terms:
                f = d.tf.get(term)
                if not f:
                    continue
                hit.add(term)
                n = df[term]
                idf = math.log(1 + (n_docs - n + 0.5) / (n + 0.5))
                score += (
                    idf
                    * f
                    * (self.k1 + 1)
                    / (f + self.k1 * (1 - self.b + self.b * d.length / avg_len))
                )
            if score > 0:
                scored.append(ScoredDoc(d.doc, score, merge_matched(query, tokens, hit)))

        def order(item: ScoredDoc) -> tuple[float, str]:
            return (-item.score, str(item.doc.get(tie_key, "")) if tie_key else "")

        scored.sort(key=order)
        return scored[:top_k]


Render = Callable[[ScoredDoc], dict[str, Any]]


def _default_render(hit: ScoredDoc) -> dict[str, Any]:
    return {**hit.doc, "score": round(hit.score, 2), "matched_terms": hit.matched_terms}


class KeywordKnowledge:
    """An Agno ``knowledge_retriever`` over documents loaded from anywhere.

    ``load`` returns every document (plain mappings); results are cached until
    :meth:`invalidate`. ``filter_fields`` are the keys the model may filter on;
    anything else, or a value no document has, is dropped with a warning rather
    than silently returning nothing. Pass ``search`` as the agent's
    ``knowledge_retriever`` and turn on ``enable_agentic_knowledge_filters``.
    """

    def __init__(
        self,
        load: Callable[[], Iterable[Mapping[str, Any]]],
        *,
        fields: Mapping[str, float],
        id_field: str = "id",
        filter_fields: Sequence[str] = (),
        include: Callable[[Mapping[str, Any]], bool] | None = None,
        render: Render | None = None,
        max_results: int = 5,
    ) -> None:
        self._load = load
        self.scorer = CjkKeywordScorer(fields)
        self.id_field = id_field
        self.filter_fields = tuple(filter_fields)
        self._include = include
        self._render = render or _default_render
        self.max_results = max_results
        self._docs: list[Mapping[str, Any]] | None = None
        self._lock = Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._docs = None

    def documents(self) -> list[Mapping[str, Any]]:
        with self._lock:
            if self._docs is None:
                docs = list(self._load())
                self._docs = [d for d in docs if self._include is None or self._include(d)]
            return self._docs

    def normalize_filter_value(self, key: str, value: Any) -> Any:
        """Map what the model wrote to a stored value. Override for synonyms (一般 → general)."""
        return value

    def filter_values(self, key: str) -> set[str]:
        return {str(d.get(key)) for d in self.documents() if d.get(key) not in (None, "")}

    def validate_filters(
        self, filters: Mapping[str, Any] | None
    ) -> tuple[dict[str, list[str]], list[str]]:
        """Keep filters on known keys with values that exist; return (valid, dropped)."""
        valid: dict[str, list[str]] = {}
        dropped: list[str] = []
        if not filters:
            return valid, dropped
        for key, raw in filters.items():
            if key not in self.filter_fields:
                dropped.append(key)
                continue
            values = raw if isinstance(raw, (list, tuple, set)) else [raw]
            known = self.filter_values(key)
            kept = [
                str(v)
                for v in (self.normalize_filter_value(key, v) for v in values)
                if str(v) in known
            ]
            if kept:
                valid[key] = kept
            else:
                dropped.append(f"{key}={raw}")
        return valid, dropped

    def search(
        self,
        query: str,
        num_documents: int | None = None,
        filters: Mapping[str, Any] | None = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        """Top documents for ``query``, each with ``score`` and ``matched_terms``."""
        valid, dropped = self.validate_filters(filters if isinstance(filters, Mapping) else None)
        if dropped:
            log.warning("keyword knowledge: ignored filters %s", dropped)
        docs = [d for d in self.documents() if all(str(d.get(k)) in vs for k, vs in valid.items())]
        limit = self.max_results if num_documents is None else num_documents
        hits = self.scorer.rank(docs, query.strip(), top_k=limit, tie_key=self.id_field)
        return [self._render(hit) for hit in hits]
