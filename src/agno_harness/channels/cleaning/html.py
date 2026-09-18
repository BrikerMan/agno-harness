"""HTML to Markdown converter for IM platforms (Teams, Webhooks, HTML payloads).

Zero-external-dependency parser based on stdlib `html.parser.HTMLParser`.
Transforms incoming chat message HTML into clean, compact, context-preserving
Markdown ready for LLM consumption:

- `<at>Name</at>` or `<at id="...">Name</at>` -> `<at>Name</at>` (literal Teams mention tag)
- `<img itemid="X">` -> `![图片](itemid:X)`
- `<emoji alt="...">` -> `...` (or fallback)
- `<p>`, `<div>` -> paragraph boundaries (`\\n\\n`)
- `<strong>`, `<b>` -> `**...**`
- `<em>`, `<i>` -> `*...*`
- `<code>` -> `` `...` ``
- `<pre>` -> ```` ```\n...\n``` ````
- `<h1>`..`<h6>` -> `#`..`######`
- `<ul>`, `<ol>`, `<li>` -> `- ` or `1. `
- `<blockquote>` -> `> `
- `<hr>` -> `---`
- `<br>` -> `\\n`
- `&nbsp;` -> space
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from html.parser import HTMLParser
from typing import Any

log = logging.getLogger("agno_harness.channels.cleaning.html")

_PARA_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "figure",
        "figcaption",
        "dd",
        "dt",
        "tr",
        "table",
        "tbody",
        "thead",
        "tfoot",
    }
)

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_LIST_TAGS = frozenset({"ul", "ol"})
_SKIP_TAGS = frozenset({"script", "style", "attachment"})

_CARRIAGE_RE = re.compile(r"\r\n?")
_HSPACE_RE = re.compile(r"[ \t\f\v]+")
_NL_SPACES_RE = re.compile(r" *\n *")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_TAG_FALLBACK_RE = re.compile(r"<[^>]+>")

_HEADING_PREFIX = {
    "h1": "#",
    "h2": "##",
    "h3": "###",
    "h4": "####",
    "h5": "#####",
    "h6": "######",
}

_HOSTED_CONTENT_RE = re.compile(r"/hostedContents/([^/]+)/\$value")
_OBJECT_ID_RE = re.compile(r"/objects/([^/\s]+)/")


def _first_attr(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    lowered = name.lower()
    for key, value in attrs:
        if key.lower() == lowered and value is not None:
            return value
    return None


def _decode_b64_token(token: str) -> str | None:
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(padded).decode("utf-8", errors="replace")
        except ValueError:
            continue
    return None


def _image_ref(attrs: list[tuple[str, str | None]]) -> str:
    itemid = _first_attr(attrs, "itemid")
    if itemid:
        return f"itemid:{itemid}"

    src = _first_attr(attrs, "src")
    if src:
        hosted = _HOSTED_CONTENT_RE.search(src)
        if hosted:
            decoded = _decode_b64_token(hosted.group(1))
            if decoded:
                obj = _OBJECT_ID_RE.search(decoded)
                if obj:
                    return f"itemid:{obj.group(1)}"
        return "sha:" + hashlib.sha256(src.encode("utf-8", errors="replace")).hexdigest()[:10]

    return "unknown"


class _HTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._skip_depth: int = 0
        self._pre_depth: int = 0
        self._list_stack: list[tuple[str, int]] = []
        self._href_stack: list[str | None] = []

    def _append(self, text: str) -> None:
        if text:
            self._pieces.append(text)

    def _is_empty(self) -> bool:
        return all(not p for p in self._pieces)

    def _trailing_newline_count(self) -> int:
        count = 0
        for piece in reversed(self._pieces):
            if not piece:
                continue
            i = len(piece)
            while i > 0 and piece[i - 1] == "\n":
                count += 1
                i -= 1
            if i > 0:
                break
        return count

    def _ensure_newline(self) -> None:
        if not self._is_empty() and self._trailing_newline_count() < 1:
            self._pieces.append("\n")

    def _ensure_blank_line(self) -> None:
        if self._is_empty():
            return
        trailing = self._trailing_newline_count()
        if trailing < 2:
            self._pieces.append("\n" * (2 - trailing))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._on_start(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._on_start(tag, attrs)

    def _on_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if tag == "img":
            alt = _first_attr(attrs, "alt") or "图片"
            self._append(f"![{alt}]({_image_ref(attrs)})")
        elif tag == "emoji":
            emoji_alt = _first_attr(attrs, "alt")
            self._append(emoji_alt if emoji_alt else "[emoji]")
        elif tag == "systemeventmessage":
            self._append("[system event]")
        elif tag == "br":
            self._append("\n")
        elif tag == "hr":
            self._ensure_blank_line()
            self._append("---")
            self._ensure_blank_line()
        elif tag == "at":
            self._append("<at>")
        elif tag == "a":
            href = _first_attr(attrs, "href")
            self._href_stack.append(href)
            if href:
                self._append("[")
        elif tag in ("strong", "b"):
            if self._pre_depth == 0:
                self._append("**")
        elif tag in ("em", "i"):
            if self._pre_depth == 0:
                self._append("*")
        elif tag == "code":
            if self._pre_depth == 0:
                self._append("`")
        elif tag == "pre":
            self._pre_depth += 1
            self._ensure_blank_line()
            self._append("```\n")
        elif tag in _HEADING_TAGS:
            self._ensure_blank_line()
            self._append(f"{_HEADING_PREFIX[tag]} ")
        elif tag == "blockquote":
            self._ensure_blank_line()
            self._append("> ")
        elif tag in _LIST_TAGS:
            self._list_stack.append((tag, 1 if tag == "ol" else 0))
            self._ensure_newline()
        elif tag == "li":
            self._ensure_newline()
            if self._list_stack:
                kind, counter = self._list_stack[-1]
                if kind == "ol":
                    self._append(f"{counter}. ")
                    self._list_stack[-1] = (kind, counter + 1)
                else:
                    self._append("- ")
            else:
                self._append("- ")
        elif tag in _PARA_BLOCK_TAGS:
            self._ensure_blank_line()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return

        if tag == "at":
            self._append("</at>")
        elif tag == "a":
            if self._href_stack:
                href = self._href_stack.pop()
                if href:
                    self._append(f"]({href})")
        elif tag in ("strong", "b"):
            if self._pre_depth == 0:
                self._append("**")
        elif tag in ("em", "i"):
            if self._pre_depth == 0:
                self._append("*")
        elif tag == "code":
            if self._pre_depth == 0:
                self._append("`")
        elif tag == "pre":
            if self._pre_depth > 0:
                self._pre_depth -= 1
            self._ensure_newline()
            self._append("```")
            self._ensure_blank_line()
        elif tag in _HEADING_TAGS or tag == "blockquote":
            self._ensure_newline()
            self._ensure_blank_line()
        elif tag in _LIST_TAGS:
            if self._list_stack:
                self._list_stack.pop()
            self._ensure_blank_line()
        elif tag == "li":
            self._ensure_newline()
        elif tag in _PARA_BLOCK_TAGS:
            self._ensure_blank_line()
        elif tag == "br":
            self._ensure_newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or not data:
            return
        self._append(data.replace("\xa0", " "))

    def result(self) -> str:
        text = "".join(self._pieces)
        text = _CARRIAGE_RE.sub("\n", text)

        # Protect indentation and spacing inside code blocks
        parts = text.split("```")
        for i in range(0, len(parts), 2):
            parts[i] = _HSPACE_RE.sub(" ", parts[i])
            parts[i] = _NL_SPACES_RE.sub("\n", parts[i])
        text = "```".join(parts)
        text = _MULTI_NL_RE.sub("\n\n", text)
        return text.strip()


def looks_like_html(value: str | None) -> bool:
    """Check if a string contains HTML tags."""
    if not value:
        return False
    return "<" in value and ">" in value


def html_to_markdown(html_str: str | None) -> str:
    """Convert HTML string to clean, compact Markdown.

    Never raises: on malformed HTML, falls back gracefully to a regex strip.
    """
    if not html_str or not html_str.strip():
        return ""

    if not looks_like_html(html_str):
        # Plain text passes through with light newline normalization
        return html_str.strip()

    parser = _HTMLToMarkdownParser()
    try:
        parser.feed(html_str)
        parser.close()
        text = parser.result()
    except Exception:
        log.warning("html_to_markdown: parse error, fallback to regex strip", exc_info=True)
        stripped = _TAG_FALLBACK_RE.sub("", html_str)
        text = _NL_SPACES_RE.sub("\n", _HSPACE_RE.sub(" ", stripped)).strip()

    return text


def teams_html_to_markdown(
    html_str: str | None,
    *,
    mentions: list[dict[str, Any]] | None = None,
) -> str:
    """Specialized Teams HTML to Markdown extractor with mention merging."""
    if not html_str or not html_str.strip():
        return ""

    if mentions:
        id_to_user: dict[str, str] = {}
        for m in mentions:
            user = (m.get("mentioned") or {}).get("user") or {}
            uid = user.get("id", "")
            if uid:
                id_to_user[str(m.get("id", ""))] = uid

        if id_to_user:
            pattern = re.compile(r'<at id="(\d+)">([^<]*)</at>&nbsp;<at id="(\d+)">([^<]*)</at>')
            prev: str | None = None
            while html_str != prev:
                prev = html_str

                def _replacer(match: re.Match[str]) -> str:
                    id1, txt1, id2, txt2 = (
                        match.group(1),
                        match.group(2),
                        match.group(3),
                        match.group(4),
                    )
                    if id_to_user.get(id1) and id_to_user.get(id1) == id_to_user.get(id2):
                        return f'<at id="{id1}">{txt1} {txt2}</at>'
                    return match.group(0)

                html_str = pattern.sub(_replacer, html_str)

    return html_to_markdown(html_str)
