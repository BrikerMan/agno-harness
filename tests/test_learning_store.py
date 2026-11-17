"""Tests for LearningStore: 3-layer files, READMEs, atomic audit, revert, doctor, and live reload."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agno.agent import Agent
from agno.models.openai.like import OpenAILike
from agno.run.base import RunContext
from agno.session import AgentSession

from agno_harness import AgentRuntime, LearningStore
from agno_harness.learning.models import LearningActor

from .conftest import FakeAgent, content, make_input, run_completed


def test_ensure_seeded_creates_all_readmes_and_preserves_existing(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seeds"
    (seed_dir / "topics").mkdir(parents=True)
    (seed_dir / "playbook.md").write_text("# Custom Playbook\n\n- Rule 1\n", encoding="utf-8")
    (seed_dir / "topics" / "games.md").write_text(
        "---\ntitle: Games\ndescription: Official game titles.\nupdated: 2026-11-17\n---\n\n- Top Heroes\n",
        encoding="utf-8",
    )

    root = tmp_path / "learning"
    hook_writes: list[str] = []
    store = LearningStore(
        root,
        seed_dir=seed_dir,
        owner_agent="moradin",
        on_after_write=hook_writes.append,
    )
    created = store.ensure_seeded()

    assert "README.md" in created
    assert "topics/README.md" in created
    assert "scopes/README.md" in created
    assert "audit/README.md" in created
    assert "playbook.md" in created
    assert "mistakes.md" in created
    assert "topics/games.md" in created

    assert "moradin" in (root / "README.md").read_text(encoding="utf-8")
    assert "INTERNAL ONLY" in (root / "scopes" / "README.md").read_text(encoding="utf-8")
    assert "INTERNAL ONLY" in (root / "audit" / "README.md").read_text(encoding="utf-8")
    assert "playbook.md" in hook_writes

    # Live edits are preserved on subsequent seed calls
    (root / "playbook.md").write_text("# Edited Playbook\n", encoding="utf-8")
    assert store.ensure_seeded() == []
    assert (root / "playbook.md").read_text(encoding="utf-8") == "# Edited Playbook\n"


def test_record_mistake_newest_first_dedupe_and_changelog(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="coord")
    actor = LearningActor(actor_id="u1", actor_name="Alice", platform="teams", chat_id="g1")

    rec1, msg1 = store.record_mistake("First rule", reason="Alice corrected", actor=actor)
    assert rec1 is not None
    assert "Recorded mistake" in msg1

    rec2, _ = store.record_mistake("Second rule", reason="Alice corrected again", actor=actor)
    assert rec2 is not None

    # Exact duplicate (case-insensitive) should no-op
    dup_rec, dup_msg = store.record_mistake("first rule", reason="dup", actor=actor)
    assert dup_rec is None
    assert "already recorded" in dup_msg

    text = store.read_text("mistakes.md") or ""
    assert text.index("Second rule") < text.index("First rule")

    changelog = store.read_text("CHANGELOG.md") or ""
    assert rec2.change_id in changelog
    assert changelog.index(rec2.change_id) < changelog.index(rec1.change_id)


def test_revert_restores_previous_or_writes_tombstone(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="coord")
    actor = LearningActor(actor_id="u1", actor_name="Eliyar")

    # 1. Modify existing file and revert
    rec_patch, _ = store.patch_document(
        "playbook.md",
        "- Verify facts with tools before asserting them.",
        "- ALWAYS verify facts with tools before asserting them.",
        reason="Strengthen verification rule",
        actor=actor,
    )
    assert "ALWAYS verify facts" in (store.read_text("playbook.md") or "")
    store.revert(rec_patch.change_id, reason="Undo strengthen", actor=actor)
    assert "ALWAYS verify facts" not in (store.read_text("playbook.md") or "")

    # 2. Create a new topic file and revert -> must write status: archived tombstone
    rec_new, _ = store.write_document(
        "topics/brand-new.md",
        "---\ntitle: Brand New\ndescription: Temp topic.\n---\n\nBody\n",
        reason="Create temp topic",
        actor=actor,
    )
    assert [t.slug for t in store.list_topics()] == ["brand-new"]

    store.revert(rec_new.change_id, reason="Created by mistake", actor=actor)
    assert store.read_text("topics/brand-new.md") is None
    assert store.list_topics() == []
    raw_disk = (tmp_path / "learning" / "topics" / "brand-new.md").read_text(encoding="utf-8")
    assert "status: archived" in raw_disk


def test_corrupted_file_fallback_and_doctor_repair(tmp_path: Path) -> None:
    root = tmp_path / "learning"
    store = LearningStore(root, owner_agent="moradin")
    actor = LearningActor(actor_id="u1", actor_name="Eliyar")

    store.patch_document(
        "playbook.md",
        "- Verify facts with tools before asserting them.",
        "- Verify live metrics before reporting.",
        reason="Update playbook",
        actor=actor,
    )

    # Simulate another agent corrupting playbook.md (unclosed frontmatter) and removing a README
    (root / "playbook.md").write_text("---\ntitle: broken\n", encoding="utf-8")
    (root / "audit" / "README.md").unlink()
    (root / "audit" / "cursors" / "bad.json").write_text("{bad json", encoding="utf-8")

    # Read-time fallback prevents crash and yields last valid snapshot
    safe_text = store.read_text("playbook.md")
    assert safe_text is not None
    assert "Verify live metrics before reporting." in safe_text

    # Diagnostic check without repair
    diag = store.doctor(repair=False)
    assert not diag.ok
    kinds = {issue.path: issue.kind for issue in diag.issues}
    assert kinds["playbook.md"] == "corrupted"
    assert kinds["audit/README.md"] == "missing"
    assert kinds["audit/cursors/bad.json"] == "corrupted"

    # Self-healing repair restores disk files cleanly
    repaired = store.doctor(repair=True)
    assert repaired.ok
    assert "Verify live metrics before reporting." in (root / "playbook.md").read_text(
        encoding="utf-8"
    )
    assert (root / "audit" / "README.md").is_file()
    assert not (root / "audit" / "cursors" / "bad.json").exists()


def test_doctor_external_drift_capture_and_restore(tmp_path: Path) -> None:
    root = tmp_path / "learning"
    store = LearningStore(root, owner_agent="moradin")
    store.ensure_seeded()

    # Valid edit made directly on disk outside LearningStore
    (root / "playbook.md").write_text(
        "# Externally Edited Playbook\n\n- Rule X\n", encoding="utf-8"
    )

    diag = store.doctor(repair=False)
    assert not diag.ok
    assert any(i.path == "playbook.md" and i.kind == "external_drift" for i in diag.issues)

    # Mode A: restore_untracked_drift=True rolls disk back to last audited snapshot
    res_restore = store.doctor(repair=True, restore_untracked_drift=True)
    assert res_restore.ok
    assert "Agent Playbook & Operating Rules" in (root / "playbook.md").read_text(encoding="utf-8")

    # Mode B: restore_untracked_drift=False captures external edit into audit chain so it's revertible
    (root / "playbook.md").write_text("# Externally Approved Edit\n", encoding="utf-8")
    res_capture = store.doctor(repair=True, restore_untracked_drift=False)
    assert res_capture.ok
    recent = store.list_changes(limit=1)[0]
    assert recent.action == "external_sync"
    assert recent.actor_id == "external:vault"
    store.revert(recent.change_id, reason="Undo unapproved external edit")
    assert "Agent Playbook & Operating Rules" in (root / "playbook.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_live_per_turn_reload_on_real_agno_agent_with_scopes(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="coord")
    agent = Agent(
        name="Coord",
        model=OpenAILike(id="gpt-4o", api_key="sk-test"),
        instructions=store.compose_instructions("Static base directive."),
        telemetry=False,
    )
    session = AgentSession(session_id="s-1")
    ctx_group_a = RunContext(
        run_id="r-1",
        session_id="s-1",
        user_id="alice",
        metadata={
            "platform": "teams",
            "chat_id": "group-a",
            "sender_id": "alice",
            "sender_name": "Alice",
            "is_direct_message": False,
        },
    )
    ctx_group_b = RunContext(
        run_id="r-2",
        session_id="s-2",
        user_id="bob",
        metadata={
            "platform": "teams",
            "chat_id": "group-b",
            "sender_id": "bob",
            "sender_name": "Bob",
            "is_direct_message": False,
        },
    )

    msg1 = await agent.aget_system_message(session=session, run_context=ctx_group_a)
    assert msg1 is not None
    assert "Never use passive voice" not in str(msg1.content)

    # Mutate Layer 1, Layer 2, and Layer 3 WITHOUT rebuilding the Agent
    store.record_mistake("Never use passive voice", reason="Alice corrected")
    store.write_document(
        "topics/style.md",
        "---\ntitle: Style\ndescription: Tone & formatting rules.\n---\n\nKeep bullets tight.\n",
        reason="Add style topic",
    )
    store.update_preference(
        "group", "group-a", "Use bullet summaries in English.", reason="Group A"
    )
    store.update_preference("user", "alice", "Address Alice directly.", reason="Alice DM/group")

    msg2_a = await agent.aget_system_message(session=session, run_context=ctx_group_a)
    assert msg2_a is not None
    content_a = str(msg2_a.content)
    assert "Never use passive voice" in content_a
    assert "topics/style.md" in content_a
    assert "Use bullet summaries in English." in content_a
    assert "Address Alice directly." in content_a

    # Group B sees global rules & catalog, NEVER Group A or Alice preferences
    msg2_b = await agent.aget_system_message(session=session, run_context=ctx_group_b)
    assert msg2_b is not None
    content_b = str(msg2_b.content)
    assert "Never use passive voice" in content_b
    assert "topics/style.md" in content_b
    assert "Use bullet summaries in English." not in content_b
    assert "Address Alice directly." not in content_b


@pytest.mark.asyncio
async def test_agent_runner_forwards_metadata_into_run_context() -> None:
    captured: dict[str, Any] = {}

    class InspectAgent(FakeAgent):
        def arun(self, **kwargs: Any):
            captured["run_context"] = kwargs.get("run_context")
            return super().arun(**kwargs)

    agent = InspectAgent([content("ok"), run_completed()])
    runtime = AgentRuntime(agent=agent)
    events = [
        e
        async for e in runtime.stream_events(
            make_input("hello", thread_id="t-meta", run_id="r-meta"),
            user_id="u-123",
            metadata={
                "platform": "teams",
                "chat_id": "19:abc@thread.v2",
                "sender_id": "u-123",
                "sender_name": "Eliyar",
                "is_direct_message": False,
            },
        )
    ]
    assert events
    rc = captured["run_context"]
    assert rc is not None
    assert rc.metadata["chat_id"] == "19:abc@thread.v2"
    assert rc.metadata["sender_id"] == "u-123"
    assert rc.metadata["is_direct_message"] is False
