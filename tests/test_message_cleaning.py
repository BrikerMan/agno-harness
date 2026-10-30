"""Tests for message HTML and rich text cleaning engines across Teams and Lark."""

from __future__ import annotations

from agno_harness import (
    html_to_markdown,
    lark_post_to_markdown,
    teams_html_to_markdown,
)
from agno_harness.channels.cleaning import clean_lark_mentions, looks_like_html


def test_looks_like_html():
    assert looks_like_html("<p>hello</p>") is True
    assert looks_like_html("plain text") is False
    assert looks_like_html(None) is False


def test_html_to_markdown_formatting():
    html = (
        "<p>Hello <strong>bold</strong> and <em>italic</em> world!</p>"
        "<p>Here is <code>inline code</code> and a link: <a href='https://example.com'>Link</a></p>"
        "<h1>Heading 1</h1>"
        "<ul><li>Item 1</li><li>Item 2</li></ul>"
        "<ol><li>First</li><li>Second</li></ol>"
        "<blockquote>Quote text</blockquote>"
        "<pre><code>def foo():\n    return 42</code></pre>"
    )
    md = html_to_markdown(html)
    assert "**bold**" in md
    assert "*italic*" in md
    assert "`inline code`" in md
    assert "[Link](https://example.com)" in md
    assert "# Heading 1" in md
    assert "- Item 1" in md
    assert "- Item 2" in md
    assert "1. First" in md
    assert "2. Second" in md
    assert "> Quote text" in md
    assert "```\ndef foo():\n    return 42\n```" in md


def test_teams_html_mentions_and_images():
    html = (
        '<p><at id="0">Arpat</at>&nbsp;<at id="1">Eliyar</at> please check this</p>'
        '<p><img src="https://graph.microsoft.com/v1.0/chats/19:x/messages/hostedContents/aW1hZ2U=/$value" '
        'itemid="img-12345" alt="diagram"></img></p>'
    )
    md = teams_html_to_markdown(html)
    assert "<at>Arpat</at>" in md
    assert "![diagram](itemid:img-12345)" in md


def test_teams_split_mentions_merging():
    # When Teams splits a single user's name across two <at> tags with identical user id
    html = '<at id="0">John</at>&nbsp;<at id="1">Doe</at> hello'
    mentions = [
        {"id": 0, "mentioned": {"user": {"id": "usr-1"}}},
        {"id": 1, "mentioned": {"user": {"id": "usr-1"}}},
    ]
    md = teams_html_to_markdown(html, mentions=mentions)
    assert "<at>John Doe</at>" in md


def test_lark_post_to_markdown():
    post_payload = {
        "zh_cn": {
            "title": "Release Notes",
            "content": [
                [
                    {"tag": "text", "text": "Version 2.0 is ", "style": []},
                    {"tag": "text", "text": "live!", "style": ["bold"]},
                    {"tag": "a", "text": "Read more", "href": "https://example.com/blog"},
                ],
                [
                    {"tag": "at", "user_id": "ou_12345", "user_name": "Bob"},
                    {"tag": "text", "text": " please review.", "style": ["italic"]},
                ],
                [
                    {"tag": "img", "image_key": "img_v2_release_key"},
                ],
            ],
        }
    }
    md = lark_post_to_markdown(post_payload)
    assert "### Release Notes" in md
    assert "Version 2.0 is **live!**" in md
    assert "[Read more](https://example.com/blog)" in md
    assert "<at>Bob</at>" in md
    assert "*please review.*" in md
    assert "![图片](image_key:img_v2_release_key)" in md


def test_clean_lark_mentions():
    text = "@_user_1 can you help @_user_2?"
    mentions = [
        {"key": "@_user_1", "name": "Alice"},
        {"key": "@_user_2", "name": "Charlie"},
    ]
    cleaned = clean_lark_mentions(text, mentions)
    assert cleaned == "<at>Alice</at> can you help <at>Charlie</at>?"


def test_teams_mention_span_conversion():
    html = '<p><span itemtype="http://schema.skype.com/Mention" itemscope="" itemid="0">Eliyar Eziz</span> please review</p>'
    md = teams_html_to_markdown(html)
    assert md == "<at>Eliyar Eziz</at> please review"


def test_teams_truncated_url_recovered():
    full_url = "https://app.notion.com/p/CN-How-Chinese-Game-Developers-and-Global-IPs-Can-Build-the-Next-Generation-of-Hit-Games-409e36a89a2b804790d1ca56c47f0e2e?source=copy_link"
    truncated_text = "https://app.notion.com/p/CN-How-Chinese-Game-Developers-and-Global-IPs-Can-Build-the-Next-Generatio…"
    html = f'<p>Here is the draft : <a href="{full_url}">{truncated_text}</a> check it out</p>'
    md = teams_html_to_markdown(html)
    assert md == f"Here is the draft : [{truncated_text}]({full_url}) check it out"


def test_teams_named_link_preserved():
    url = "https://yodo1net-my.sharepoint.com/:w:/g/personal/test/file.docx"
    html = f'<p>Please see <a href="{url}">AMC press release.docx</a> now</p>'
    md = teams_html_to_markdown(html)
    assert md == f"Please see [AMC press release.docx]({url}) now"


def test_teams_reply_blockquote_conversion():
    html = (
        '<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="1793267821469">'
        '<strong itemprop="mri" itemid="28:abc">Display Name</strong>'
        '<span itemprop="time" itemid="1793267821469"></span>'
        '<p itemprop="preview">Eliyar Eziz: Moradin try again\nLine 2</p>'
        "</blockquote>"
        "<p>let me check that</p>"
    )
    md = teams_html_to_markdown(html)
    assert '<quoted messageId="1793267821469">' in md
    assert "> **Display Name:** Eliyar Eziz: Moradin try again" in md
    assert "> Line 2" in md
    assert "</quoted>" in md
    assert "let me check that" in md


def test_teams_reply_multiline_and_sender_dedup():
    html = (
        '<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="1793267821469">'
        '<strong itemprop="mri">Eliyar Eziz</strong>'
        '<p itemprop="preview">Eliyar Eziz: Hello Moradin<br>Please look at item A<br>And item B</p>'
        "</blockquote>"
        "<p>I am working on it</p>"
    )
    md = teams_html_to_markdown(html)
    assert (
        '<quoted messageId="1793267821469">\n'
        "> **Eliyar Eziz:** Hello Moradin\n"
        "> Please look at item A\n"
        "> And item B\n"
        "</quoted>"
    ) in md
    assert "I am working on it" in md


def test_recover_teams_html_prefers_attachment_with_links():
    from agno_harness.channels.cleaning import recover_teams_html

    plain_body = "<p>Here is the document without link</p>"
    rich_attachment = [
        {
            "contentType": "text/html",
            "content": '<p>Here is the document with <a href="https://example.com/doc">link</a></p>',
        }
    ]
    recovered = recover_teams_html(attachments=rich_attachment, body_html=plain_body)
    assert "https://example.com/doc" in recovered
