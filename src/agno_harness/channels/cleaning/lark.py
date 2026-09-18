"""Lark (Feishu) message payload cleaning and Markdown extraction.

Handles Lark rich text `post` payloads, text with `@_user_` mentions,
and embeds standard Markdown structures.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .html import html_to_markdown, looks_like_html

log = logging.getLogger("agno_harness.channels.cleaning.lark")


def lark_post_to_markdown(post_content: dict[str, Any] | str) -> str:
    """Convert Lark `post` message format to standard Markdown.

    Structure of Lark post:
    {
      "zh_cn": {
        "title": "Title",
        "content": [
          [
            {"tag": "text", "text": "Hello ", "style": ["bold"]},
            {"tag": "a", "text": "Link", "href": "https://example.com"},
            {"tag": "at", "user_id": "ou_xxx", "user_name": "Alice"}
          ],
          [
            {"tag": "img", "image_key": "img_xxx"}
          ]
        ]
      }
    }
    """
    raw_str: str | None = post_content if isinstance(post_content, str) else None
    if raw_str is not None:
        try:
            parsed = json.loads(raw_str)
            if isinstance(parsed, dict):
                post_content = parsed
            else:
                return str(parsed)
        except Exception:
            if looks_like_html(raw_str):
                return html_to_markdown(raw_str)
            return raw_str.strip()

    if not isinstance(post_content, dict):
        return ""

    # Locate language block (prefer zh_cn, en_us, default or first available)
    lang_block = None
    for preferred in ("zh_cn", "en_us", "default"):
        if preferred in post_content and isinstance(post_content[preferred], dict):
            lang_block = post_content[preferred]
            break

    if lang_block is None:
        for val in post_content.values():
            if isinstance(val, dict) and "content" in val:
                lang_block = val
                break

    if not lang_block or not isinstance(lang_block, dict):
        # Might already be the content block directly
        lang_block = post_content

    paragraphs_data = lang_block.get("content")
    title = lang_block.get("title", "").strip()

    lines: list[str] = []
    if title:
        lines.append(f"### {title}\n")

    if isinstance(paragraphs_data, list):
        for para in paragraphs_data:
            if not isinstance(para, list):
                continue
            para_pieces: list[str] = []
            for elem in para:
                if not isinstance(elem, dict):
                    continue
                tag = elem.get("tag", "")
                text = elem.get("text", "")
                styles = elem.get("style", []) or []

                if tag == "text":
                    formatted = text
                    leading = " " if text.startswith(" ") else ""
                    trailing = " " if text.endswith(" ") and len(text) > 1 else ""
                    stripped = text.strip()
                    if stripped and styles:
                        if "code" in styles:
                            stripped = f"`{stripped}`"
                        if "bold" in styles:
                            stripped = f"**{stripped}**"
                        if "italic" in styles:
                            stripped = f"*{stripped}*"
                        if "strike_through" in styles:
                            stripped = f"~~{stripped}~~"
                        formatted = f"{leading}{stripped}{trailing}"
                    para_pieces.append(formatted)
                elif tag == "a":
                    href = elem.get("href", "")
                    para_pieces.append(f"[{text or href}]({href})")
                elif tag == "at":
                    user_name = elem.get("user_name") or elem.get("user_id") or "someone"
                    para_pieces.append(f"<at>{user_name}</at>")
                elif tag == "img":
                    image_key = elem.get("image_key", "")
                    para_pieces.append(f"![图片](image_key:{image_key})")
                elif tag == "emotion":
                    emoji_key = elem.get("emoji_type") or elem.get("emotion_key") or "emoji"
                    para_pieces.append(f"[{emoji_key}]")
                elif tag == "code_block":
                    code_text = elem.get("text", "")
                    language = elem.get("language", "")
                    para_pieces.append(f"\n```{language}\n{code_text}\n```\n")
                elif tag == "hr":
                    para_pieces.append("\n---\n")
                else:
                    if text:
                        para_pieces.append(text)

            line = "".join(para_pieces).strip()
            if line:
                lines.append(line)

    return "\n\n".join(lines).strip()


def clean_lark_mentions(text: str, mentions: list[dict[str, Any]] | None) -> str:
    """Replace Lark placeholders like `@_user_1` with `<at>UserName</at>`."""
    if not text or not mentions:
        return text or ""

    for m in mentions:
        key = m.get("key", "")
        name = m.get("name", "")
        if key and name:
            text = text.replace(key, f"<at>{name}</at>")

    return text
