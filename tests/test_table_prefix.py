"""Table names share one prefix. The default is agno_harness_."""

import pytest
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agno_harness.stores import (
    apply_table_prefix,
    harness_metadata,
    include_harness,
    numbered_revisions,
    register_harness_models,
    table_name,
)
from agno_harness.stores.prefix import resolve_prefix
from agno_harness.stores.sql_models import get_or_create_session_model


class _Base(DeclarativeBase):
    pass


def test_the_default_prefix_is_agno_harness():
    assert table_name("sessions") == "agno_harness_sessions"
    assert table_name("runs") == "agno_harness_runs"
    assert get_or_create_session_model().__tablename__ == "agno_harness_conversation_sessions"


def test_a_caller_can_choose_user_agent_or_admin_agent():
    assert table_name("sessions", prefix="user-agent") == "user-agent-sessions"
    assert table_name("sessions", prefix="admin_agent") == "admin_agent_sessions"
    assert resolve_prefix("user-agent-") == "user-agent"
    assert resolve_prefix("admin_agent_") == "admin_agent"


def test_a_prefix_with_spaces_or_punctuation_is_rejected():
    with pytest.raises(ValueError):
        table_name("sessions", prefix="bad agent")


def test_apply_table_prefix_renames_the_agno_tables_that_exist():
    class Db:
        session_table_name = "agno_sessions"
        runs_table_name = "agno_runs"
        tool_results_table_name = "agno_tool_results"

    db = Db()
    apply_table_prefix(db, "user-agent")
    assert db.session_table_name == "user-agent-sessions"
    assert db.runs_table_name == "user-agent-runs"
    assert db.tool_results_table_name == "user-agent-tool-results"

    plain = Db()
    apply_table_prefix(plain)
    assert plain.session_table_name == "agno_harness_sessions"


def test_register_harness_models_declares_every_table_under_one_prefix():
    models = register_harness_models("admin-agent", _Base)
    names = {model.__tablename__ for model in models.values()}
    assert names == {
        "admin-agent-threads",
        "admin-agent-conversation-sessions",
        "admin-agent-actions",
        "admin-agent-message-audits",
        "admin-agent-custom-events",
        "admin-agent-run-frames",
        "admin-agent-run-records",
        "admin-agent-run-archives",
    }
    assert "admin-agent-run-frames" in _Base.metadata.tables


def test_harness_metadata_is_ready_for_alembic():
    metadata = harness_metadata("user-agent", _Base)
    assert metadata is _Base.metadata
    assert "user-agent-conversation-sessions" in metadata.tables
    assert "user-agent-run-archives" in metadata.tables


def test_harness_metadata_reads_the_env_prefix(monkeypatch):
    class EnvBase(DeclarativeBase):
        pass

    monkeypatch.setenv("AGNO_HARNESS_TABLE_PREFIX", "admin-agent")
    metadata = harness_metadata(base=EnvBase)
    assert "admin-agent-actions" in metadata.tables


def test_register_on_the_project_base_keeps_target_metadata():
    class AppBase(DeclarativeBase):
        pass

    class Note(AppBase):
        __tablename__ = "notes"
        id: Mapped[int] = mapped_column(primary_key=True)

    register_harness_models("user-agent", AppBase)
    target_metadata = AppBase.metadata
    assert "notes" in target_metadata.tables
    assert "user-agent-message-audits" in target_metadata.tables


def test_include_harness_keeps_the_project_tables():
    class AppBase(DeclarativeBase):
        pass

    class Note(AppBase):
        __tablename__ = "notes"
        id: Mapped[int] = mapped_column(primary_key=True)

    combined = include_harness(AppBase.metadata, "user-agent")
    assert combined[0] is AppBase.metadata
    assert "notes" in combined[0].tables
    assert "user-agent-conversation-sessions" in combined[1].tables
    assert "notes" not in combined[1].tables


def test_include_harness_appends_to_an_existing_list():
    class AppBase(DeclarativeBase):
        pass

    class ExtraBase(DeclarativeBase):
        pass

    combined = include_harness([AppBase.metadata, ExtraBase.metadata], "admin-agent")
    assert combined[:2] == [AppBase.metadata, ExtraBase.metadata]
    assert "admin-agent-actions" in combined[2].tables
    again = include_harness(combined, "admin-agent")
    assert len(again) == 3
    assert again[2] is combined[2]


def test_numbered_revisions_counts_from_0001():
    class Script:
        rev_id = ""

    first = Script()
    numbered_revisions(None, (None,), [first])
    assert first.rev_id == "0001"
    second = Script()
    numbered_revisions(None, ("0001",), [second])
    assert second.rev_id == "0002"


def test_numbered_revisions_leaves_a_hash_head_alone():
    class Script:
        rev_id = "kept"

    script = Script()
    numbered_revisions(None, ("a1b2c3d4e5f6",), [script])
    assert script.rev_id == "kept"
