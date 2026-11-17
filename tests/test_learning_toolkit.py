"""Tests for LearningToolkit, scope isolation, privacy filtering, and scaffold seeds."""

from __future__ import annotations

from pathlib import Path

from agno.run.base import RunContext
from typer.testing import CliRunner

from agno_harness import LearningStore, LearningToolkit
from agno_harness.cli.main import app as cli_app
from agno_harness.helpers.scaffold import copy_learning_seeds


def _ctx(
    *,
    user_id: str = "u1",
    sender_name: str = "Alice",
    chat_id: str = "g1",
    is_dm: bool = False,
) -> RunContext:
    return RunContext(
        run_id="r1",
        session_id="s1",
        user_id=user_id,
        metadata={
            "platform": "teams",
            "chat_id": chat_id,
            "sender_id": user_id,
            "sender_name": sender_name,
            "is_direct_message": is_dm,
        },
    )


def test_toolkit_read_write_patch_and_isolation(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="moradin")
    tk = LearningToolkit(store, allow_write=True)
    ctx_alice = _ctx(user_id="alice", sender_name="Alice", chat_id="group-1", is_dm=False)

    # Create a topic glossary and patch it
    res_write = tk.write_file(
        "topics/games.md",
        "---\ntitle: Games\ndescription: Game titles.\n---\n\n- Top Heroes: Top Heroes\n",
        reason="Init games topic",
        run_context=ctx_alice,
    )
    assert res_write.startswith("Successfully wrote ")

    res_patch = tk.patch_file(
        "topics/games.md",
        "- Top Heroes: Top Heroes",
        "- Top Heroes: 消消英雄",
        reason="Official CN name",
        run_context=ctx_alice,
    )
    assert res_patch.startswith("Successfully patched ")
    assert "消消英雄" in tk.read("topics/games.md")
    assert "topics/games.md" in tk.grep("消消英雄")
    assert "topics/games.md" in tk.list_files("topics/*.md")

    # Update user & group preferences
    pref_group = tk.update_preference(
        "group", "Reply in concise bullets.", reason="Group preference", run_context=ctx_alice
    )
    assert "Updated group preference" in pref_group
    pref_user = tk.update_preference(
        "user", "Always include preview URLs.", reason="Alice preference", run_context=ctx_alice
    )
    assert "Updated user preference" in pref_user

    # Scopes and audit directories MUST be completely invisible and inaccessible to file tools
    assert "scopes/" not in tk.list_files()
    assert "audit/" not in tk.list_files()
    assert "Alice preference" not in tk.grep("Always include")
    assert "Security error" in tk.read("scopes/README.md")
    assert "Security error" in tk.read("audit/changes")
    assert "Security error" in tk.write_file("scopes/users/fake.md", "hack")
    assert "Security error" in tk.patch_file("audit/README.md", "Audit", "Hacked")
    assert "Security error" in tk.read("../../etc/passwd")


def test_toolkit_dm_group_guard_and_learning_privacy(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="moradin")
    tk = LearningToolkit(store, allow_write=True)

    ctx_dm = _ctx(user_id="alice", sender_name="Alice", chat_id="dm-alice", is_dm=True)
    err_group = tk.update_preference("group", "Group rule", reason="bad", run_context=ctx_dm)
    assert "not available in direct messages" in err_group

    # Alice saves a private user preference
    tk.update_preference("user", "Alice secret style", reason="Alice pref", run_context=ctx_dm)
    # Bob in another group should NOT see Alice's user preference change in list_learnings
    ctx_bob = _ctx(user_id="bob", sender_name="Bob", chat_id="group-2", is_dm=False)
    bob_view = tk.list_learnings(limit=20, run_context=ctx_bob)
    assert "Alice pref" not in bob_view

    alice_view = tk.list_learnings(limit=20, run_context=ctx_dm)
    assert "Alice pref" in alice_view


def test_toolkit_read_only_mode_and_revert_repair(tmp_path: Path) -> None:
    store = LearningStore(tmp_path / "learning", owner_agent="moradin")
    tk_rw = LearningToolkit(store, allow_write=True)
    tk_ro = LearningToolkit(store, allow_write=False)

    assert set(tk_ro.functions.keys()) == {"read", "grep", "list_files", "list_learnings"}
    assert "disabled" in tk_ro.write_file("playbook.md", "x").lower()
    assert "disabled" in tk_ro.record_mistake("x", "y").lower()

    ctx = _ctx()
    tk_rw.record_mistake("Do not use em dashes", reason="Style correction", run_context=ctx)
    changes = store.list_changes(limit=5)
    mistake_change_id = changes[0].change_id

    reverted = tk_rw.revert_learning(mistake_change_id, reason="Undo", run_context=ctx)
    assert "Reverted" in reverted
    assert "Do not use em dashes" not in (store.read_text("mistakes.md") or "")

    # Corrupt file and repair via toolkit
    (tmp_path / "learning" / "playbook.md").write_text("", encoding="utf-8")
    repair_msg = tk_rw.repair_learning(run_context=ctx)
    assert "repaired" in repair_msg
    assert store.read_text("playbook.md")


def test_init_copies_learning_seeds_and_preserves_edits(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["init", str(tmp_path), "--channel", "web"])
    assert result.exit_code == 0

    live_playbook = tmp_path / "data" / "learning" / "playbook.md"
    live_mistakes = tmp_path / "data" / "learning" / "mistakes.md"
    assert live_playbook.is_file()
    assert live_mistakes.is_file()

    live_playbook.write_text("# Custom Team Playbook\n", encoding="utf-8")
    (tmp_path / "app" / "learning" / "topics").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "learning" / "topics" / "glossary.md").write_text(
        "# Glossary\n", encoding="utf-8"
    )
    copy_learning_seeds(tmp_path)

    assert live_playbook.read_text(encoding="utf-8") == "# Custom Team Playbook\n"
    assert (tmp_path / "data" / "learning" / "topics" / "glossary.md").read_text(
        encoding="utf-8"
    ) == "# Glossary\n"
