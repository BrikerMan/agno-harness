"""HTML to Markdown converter for IM platforms (Teams, Webhooks, HTML payloads).

Zero-external-dependency parser based on stdlib `html.parser.HTMLParser`.
Transforms incoming chat message HTML into clean, compact, context-preserving
Markdown ready for LLM consumption:

- `<at>Name</at>` or `<span itemtype=".../Mention">Name</span>` -> `<at>Name</at>` (literal Teams mention tag)
- `<img itemid="X">` -> `![图片](itemid:X)`
- `<emoji alt="...">` -> `...` (or fallback)
- `<a href="...">text</a>` -> `[text](url)` or `url` (recovers full URL from truncated URLs or anchor text)
- `<blockquote itemtype=".../Reply">` -> formatted quote reply with `<quoted messageId="..."/>` and `> **Sender:** preview`
- `<p>`, `<div>` -> paragraph boundaries (`\n\n`)
- `<strong>`, `<b>` -> `**...**`
- `<em>`, `<i>` -> `*...*`
- `<code>` -> `` `...` ``
- `<pre>` -> ```` ```\n...\n``` ````
- `<h1>`..`<h6>` -> `#`..`######`
- `<ul>`, `<ol>`, `<li>` -> `- ` or `1. `
- `<blockquote>` -> `> `
- `<hr>` -> `---`
- `<br>` -> `\n`
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
_HREF_RE = re.compile(r"""href\s*=\s*["'](https?://[^"']+)["']""", re.IGNORECASE)
_HTTP_URL_RE = re.compile(r"https?://[^\s\]\)<>\"']+", re.IGNORECASE)
_QUOTED_PREFIX_RE = re.compile(
    r"""^(<quoted(?:\s+messageId="[^"]+")?\s*>.*?</quoted>|<quoted\s+messageId="[^"]+"\s*/>)\s*""",
    re.IGNORECASE | re.DOTALL,
)
_EMPTY_MD = "[empty message]"


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


def _canonical_url(url: str) -> str:
    return url.rstrip("/")


def http_urls_in(value: str | None) -> set[str]:
    """Canonical http(s) URLs from markdown, plain text, or HTML hrefs."""
    found: set[str] = set()
    if not value:
        return found
    for match in _HTTP_URL_RE.finditer(value):
        found.add(match.group(0).rstrip(".,;"))
    for match in _HREF_RE.finditer(value):
        found.add(match.group(1).rstrip(".,;"))
    return {_canonical_url(url) for url in found}


class _HTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._skip_depth: int = 0
        self._pre_depth: int = 0
        self._list_stack: list[tuple[str, int]] = []
        # Each entry: (href, piece_start_index)
        self._href_stack: list[tuple[str | None, int]] = []
        self._mention_span_depth: int = 0

        # Quote handling
        self._quote_depth: int = 0
        self._in_reply_quote: bool = False
        self._reply_itemid: str = ""
        self._reply_sender: str = ""
        self._reply_preview: list[str] = []
        self._reply_part: str = ""

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

        if self._in_reply_quote:
            itemprop = (_first_attr(attrs, "itemprop") or "").lower()
            if itemprop == "mri":
                self._reply_part = "sender"
            elif itemprop == "preview":
                self._reply_part = "preview"
            elif tag in ("br", "p", "div") and self._reply_part == "preview":
                self._reply_preview.append("\n")
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
            self._ensure_newline()
            if self._quote_depth > 0:
                self._append("> ")
        elif tag == "hr":
            self._ensure_blank_line()
            self._append("---")
            self._ensure_blank_line()
        elif tag == "at":
            self._append("<at>")
        elif tag == "span":
            itemtype = (_first_attr(attrs, "itemtype") or "").lower()
            if "schema.skype.com/mention" in itemtype or "mention" in itemtype:
                self._mention_span_depth += 1
                self._append("<at>")
        elif tag == "a":
            href = _first_attr(attrs, "href")
            self._href_stack.append((href, len(self._pieces)))
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
            itemtype = (_first_attr(attrs, "itemtype") or "").lower()
            if "schema.skype.com/reply" in itemtype or "reply" in itemtype:
                self._in_reply_quote = True
                self._reply_itemid = _first_attr(attrs, "itemid") or ""
                self._reply_sender = ""
                self._reply_preview = []
                self._reply_part = ""
            else:
                self._quote_depth += 1
                self._ensure_blank_line()
                self._append("> ")
        elif tag in _LIST_TAGS:
            self._list_stack.append((tag, 1 if tag == "ol" else 0))
            self._ensure_newline()
        elif tag == "li":
            self._ensure_newline()
            if self._quote_depth > 0:
                self._append("> ")
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
            if self._quote_depth > 0:
                self._append("> ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return

        if self._in_reply_quote:
            if tag == "blockquote":
                self._in_reply_quote = False
                self._reply_part = ""
                self._ensure_blank_line()
                sender = self._reply_sender.strip()
                preview = "".join(self._reply_preview).strip()

                if self._reply_itemid:
                    self._append(f'<quoted messageId="{self._reply_itemid}">\n')
                else:
                    self._append("<quoted>\n")

                body = ""
                if sender and preview:
                    sender_prefix = f"{sender}:"
                    if preview.lower().startswith(sender_prefix.lower()):
                        preview_body = preview[len(sender_prefix) :].lstrip()
                        body = f"**{sender}:** {preview_body}" if preview_body else f"**{sender}**"
                    else:
                        body = f"**{sender}:** {preview}"
                elif preview:
                    body = preview
                elif sender:
                    body = f"**{sender}**"

                if body:
                    for line in body.splitlines():
                        line_str = line.strip()
                        if line_str:
                            self._append(f"> {line_str}\n")
                self._append("</quoted>\n\n")
            elif tag in ("p", "div") and self._reply_part == "preview":
                self._reply_preview.append("\n")
                self._reply_part = ""
            elif tag in ("strong", "p", "span"):
                self._reply_part = ""
            return

        if tag == "at":
            self._append("</at>")
        elif tag == "span" and self._mention_span_depth > 0:
            self._mention_span_depth -= 1
            self._append("</at>")
        elif tag == "a":
            if self._href_stack:
                href, start_idx = self._href_stack.pop()
                if href:
                    inner_text = "".join(self._pieces[start_idx:]).strip()
                    del self._pieces[start_idx:]
                    if inner_text:
                        self._pieces.append(f"[{inner_text}]({href})")
                    else:
                        self._pieces.append(f"[{href}]({href})")
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
        elif tag in _HEADING_TAGS:
            self._ensure_newline()
            self._ensure_blank_line()
        elif tag == "blockquote":
            if self._quote_depth > 0:
                self._quote_depth -= 1
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
        data = data.replace("\xa0", " ")
        if self._in_reply_quote:
            if self._reply_part == "sender":
                self._reply_sender += data
            elif self._reply_part == "preview":
                self._reply_preview.append(data)
        else:
            self._append(data)

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
            # Merge consecutive split <at> tags
            pattern_at = re.compile(r'<at id="(\d+)">([^<]*)</at>&nbsp;<at id="(\d+)">([^<]*)</at>')
            prev: str | None = None
            while html_str != prev:
                prev = html_str

                def _replacer_at(match: re.Match[str]) -> str:
                    id1, txt1, id2, txt2 = (
                        match.group(1),
                        match.group(2),
                        match.group(3),
                        match.group(4),
                    )
                    if id_to_user.get(id1) and id_to_user.get(id1) == id_to_user.get(id2):
                        return f'<at id="{id1}">{txt1} {txt2}</at>'
                    return match.group(0)

                html_str = pattern_at.sub(_replacer_at, html_str)

            # Merge consecutive split <span itemtype="...Mention"> tags
            pattern_span = re.compile(
                r'<span\s+itemtype="[^"]*Mention"[^>]*itemid="(\d+)"[^>]*>([^<]*)</span>&nbsp;<span\s+itemtype="[^"]*Mention"[^>]*itemid="(\d+)"[^>]*>([^<]*)</span>',
                re.IGNORECASE,
            )
            prev = None
            while html_str != prev:
                prev = html_str

                def _replacer_span(match: re.Match[str]) -> str:
                    id1, txt1, id2, txt2 = (
                        match.group(1),
                        match.group(2),
                        match.group(3),
                        match.group(4),
                    )
                    if id_to_user.get(id1) and id_to_user.get(id1) == id_to_user.get(id2):
                        return f'<span itemtype="http://schema.skype.com/Mention" itemscope="" itemid="{id1}">{txt1} {txt2}</span>'
                    return match.group(0)

                html_str = pattern_span.sub(_replacer_span, html_str)

    return html_to_markdown(html_str)


def html_from_teams_attachments(attachments: Any) -> str:
    """Richest ``text/html`` attachment body (prefer the one with the most hrefs/length)."""
    if not isinstance(attachments, list):
        return ""
    best = ""
    best_hrefs = -1
    for att in attachments:
        if not isinstance(att, dict):
            continue
        ctype = str(att.get("contentType") or att.get("content_type") or "").lower()
        if not ctype.startswith("text/html"):
            continue
        content = att.get("content")
        if not isinstance(content, str) or not looks_like_html(content):
            continue
        href_count = len(_HREF_RE.findall(content))
        if href_count > best_hrefs or (href_count == best_hrefs and len(content) > len(best)):
            best = content
            best_hrefs = href_count
    return best


def recover_teams_html(
    *,
    attachments: Any = None,
    body_html: str | None = None,
    raw_html: str | None = None,
) -> str:
    """Pick the HTML source that still has links (Teams puts hrefs in text/html attachments)."""
    candidates = [
        html_from_teams_attachments(attachments),
        body_html if looks_like_html(body_html) else "",
        raw_html if looks_like_html(raw_html) else "",
    ]
    ranked = [item for item in candidates if item]
    if not ranked:
        return ""
    return max(ranked, key=lambda html: (len(_HREF_RE.findall(html)), len(html)))


def markdown_from_teams_html(
    html: str,
    *,
    plain_text: str = "",
    mentions: list[dict[str, Any]] | None = None,
) -> str:
    """HTML -> markdown, keeping a Teams ``<quoted messageId>`` prefix from plain text if omitted."""
    md = teams_html_to_markdown(html, mentions=mentions)
    if not md or md == _EMPTY_MD:
        md = (plain_text or "").strip()
    quoted = _QUOTED_PREFIX_RE.match(plain_text or "")
    if quoted and quoted.group(1) not in md:
        md = f"{quoted.group(1)} {md}".strip()
    return md


def missing_http_hrefs(html: str, text: str | None) -> set[str]:
    """href targets in HTML that do not already appear in stored/plain text."""
    html_hrefs = {_canonical_url(match.group(1)) for match in _HREF_RE.finditer(html or "")}
    return html_hrefs - http_urls_in(text)


def recompute_markdown_if_links_missing(
    raw: dict[str, Any] | None,
    *,
    text: str | None,
    raw_text: str | None,
) -> str | None:
    """Return new markdown only when stored HTML has http(s) hrefs that ``text`` dropped."""
    if not isinstance(raw, dict):
        html = recover_teams_html(raw_html=raw_text)
    else:
        attachments: list[Any] = []
        top = raw.get("attachments")
        if isinstance(top, list):
            attachments.extend(top)
        nested = raw.get("raw")
        nested_body: Any = None
        if isinstance(nested, dict):
            nested_atts = nested.get("attachments")
            if isinstance(nested_atts, list):
                attachments.extend(nested_atts)
            nested_body = nested.get("body")

        body_html: str | None = None
        for candidate in (raw.get("body"), nested_body, raw.get("raw_html")):
            if isinstance(candidate, dict):
                content = candidate.get("content")
                if isinstance(content, str) and looks_like_html(content):
                    body_html = content
                    break
            if isinstance(candidate, str) and looks_like_html(candidate):
                body_html = candidate
                break
        html = recover_teams_html(attachments=attachments, body_html=body_html, raw_html=raw_text)

    if not html or not missing_http_hrefs(html, text):
        return None
    md = markdown_from_teams_html(html, plain_text=text or "")
    if not md or md == text:
        return None
    if not missing_http_hrefs(html, md):
        return md
    return None
