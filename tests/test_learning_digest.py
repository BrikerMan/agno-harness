"""Tests for LearningDigestor: NOOP, multi-layer extraction, cursors, and session-close hook."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agno_harness import (
    ChannelEvent,
    ConversationKey,
    InMemorySessionStore,
    InMemorySink,
    LearningDigestor,
    LearningStore,
    OutboundMessage,
    SessionManager,
    attach_session_close_digest,
)


class FakeDigestModel:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[str] = []

    async def aresponse(self, messages: list[Any], **_: Any) -> Any:
        self.calls.append(getattr(messages[-1], "content", "") if messages else "")
        reply = self.responses.pop(0) if self.responses else "NOOP"
        return type("Resp", (), {"content": reply})()


@pytest.mark.asyncio
async def test_digest_noop_and_cursor_idempotency(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="moradin")
    model = FakeDigestModel(["NOOP"])
    digestor = LearningDigestor(store, model)

    res1 = await digestor.digest_transcript(
        "User: hello\nAssistant: hi",
        source_key="chat:g1",
        cursor="msg-10",
        chat_id="g1",
    )
    assert res1 == []
    assert len(model.calls) == 1
    assert digestor.get_cursor("chat:g1") == "msg-10"

    # Re-running with the same cursor skips LLM call entirely
    res2 = await digestor.digest_transcript(
        "User: hello\nAssistant: hi",
        source_key="chat:g1",
        cursor="msg-10",
        chat_id="g1",
    )
    assert res2 == []
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_digest_extracts_mistakes_topics_and_preferences_and_dry_run(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="moradin")
    payload = json.dumps(
        {
            "updates": [
                {
                    "kind": "mistake",
                    "rule": "Never translate DeepMatch into Chinese.",
                    "reason": "Team agreed in group chat",
                },
                {
                    "kind": "topic_write",
                    "slug": "ipverse",
                    "title": "IPverse",
                    "description": "IPverse product names.",
                    "content": "- DeepMatch: keep English name.",
                },
                {
                    "kind": "group_preference",
                    "reason": "Group prefers EN replies",
                    "content": "- Reply in English by default.",
                },
                {
                    "kind": "user_preference",
                    "reason": "User prefers preview links",
                    "content": "- Always include both EN and ZH preview links.",
                },
            ]
        }
    )
    model = FakeDigestModel([f"```json\n{payload}\n```", payload])
    digestor = LearningDigestor(store, model)

    # Dry run returns preview records without writing files or advancing cursor
    dry = await digestor.digest_transcript(
        "Eliyar: Keep DeepMatch in English.",
        source_key="chat:g1",
        cursor="msg-20",
        chat_id="g1",
        user_id="eliyar",
        is_direct_message=False,
        dry_run=True,
    )
    assert len(dry) == 4
    assert all(r.change_id == "dry-run" for r in dry)
    assert digestor.get_cursor("chat:g1") is None
    assert "DeepMatch" not in (store.read_text("mistakes.md") or "")

    # Real run commits across all 3 layers and advances cursor
    real = await digestor.digest_transcript(
        "Eliyar: Keep DeepMatch in English.",
        source_key="chat:g1",
        cursor="msg-20",
        chat_id="g1",
        user_id="eliyar",
        is_direct_message=False,
        dry_run=False,
    )
    assert len(real) == 4
    assert "Never translate DeepMatch into Chinese." in (store.read_text("mistakes.md") or "")
    assert "keep English name" in (store.read_text("topics/ipverse.md") or "")
    assert "Reply in English" in store.read_preference("group", "g1")
    assert "preview links" in store.read_preference("user", "eliyar")
    assert digestor.get_cursor("chat:g1") == "msg-20"


@pytest.mark.asyncio
async def test_digest_sink_chat_and_session_close_hook(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="coord")
    payload = json.dumps(
        {
            "updates": [
                {
                    "kind": "mistake",
                    "rule": "Always cite article id in draft confirmations.",
                    "reason": "Session feedback",
                }
            ]
        }
    )
    digestor = LearningDigestor(store, FakeDigestModel([payload]))

    sink = InMemorySink()
    key = ConversationKey(
        platform="teams",
        chat_id="chat-99",
        sender_id="u-eliyar",
        is_direct_message=False,
    )
    await sink.record_inbound(
        ChannelEvent(event_id="e1", key=key, text="Please always cite article id next time."),
        session_id="s1",
    )
    await sink.record_outbound(key, OutboundMessage(text="Got it."), session_id="s1")

    session_mgr = SessionManager(store=InMemorySessionStore())
    await session_mgr.get_or_create_session(key)
    attach_session_close_digest(session_mgr, digestor, sink)

    await session_mgr.close_session(key, reason="daily")
    tasks = list(getattr(session_mgr, "_digest_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)

    assert "Always cite article id" in (store.read_text("mistakes.md") or "")
