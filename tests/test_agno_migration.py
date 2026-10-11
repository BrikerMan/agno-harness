"""Agno v2 -> v3 database migration wrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from agno.db.sqlite import SqliteDb
from typer.testing import CliRunner

from agno_harness.cli.main import app
from agno_harness.db import (
    AgnoHarnessDb,
    agno_db_needs_migration,
    migrate_agno_db,
)

LEGACY_RUN = {
    "run_id": "legacy-run-1",
    "session_id": "legacy-session",
    "agent_id": "agent",
    "status": "COMPLETED",
    "content": "hello from v2",
    "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello from v2"},
    ],
}


def _make_v2_database(db_file: Path) -> None:
    """A sessions table shaped like Agno v2: history lives in a ``runs`` JSON column."""
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE agno_sessions (
            session_id VARCHAR PRIMARY KEY,
            session_type VARCHAR NOT NULL,
            agent_id VARCHAR, team_id VARCHAR, workflow_id VARCHAR, user_id VARCHAR,
            session_data JSON, agent_data JSON, team_data JSON, workflow_data JSON,
            metadata JSON, runs JSON, summary JSON,
            created_at BIGINT NOT NULL, updated_at BIGINT
        );
        CREATE TABLE agno_schema_versions (
            table_name VARCHAR PRIMARY KEY, version VARCHAR NOT NULL,
            created_at VARCHAR, updated_at VARCHAR
        );
        """
    )
    con.execute(
        "INSERT INTO agno_sessions (session_id, session_type, agent_id, runs, created_at, updated_at)"
        " VALUES (?, 'agent', 'agent', ?, 1, 1)",
        ("legacy-session", json.dumps([LEGACY_RUN])),
    )
    con.execute("INSERT INTO agno_schema_versions VALUES ('agno_sessions', '2.5.6', 'x', 'x')")
    con.commit()
    con.close()


def test_migrate_moves_legacy_runs_and_keeps_the_blob(tmp_path: Path) -> None:
    db_file = tmp_path / "v2.db"
    _make_v2_database(db_file)
    db = SqliteDb(db_file=str(db_file))

    assert asyncio.run(agno_db_needs_migration(db)) is True

    report = asyncio.run(migrate_agno_db(db))

    assert report.version_before == "2.5.6"
    assert report.migrated is True
    assert report.sample_run_ids == ["legacy-run-1"]
    assert asyncio.run(agno_db_needs_migration(db)) is False

    runs = SqliteDb(db_file=str(db_file)).get_runs(session_id="legacy-session")
    assert [r.run_id for r in runs] == ["legacy-run-1"]

    # Non-destructive: the legacy blob is still there as a backup.
    con = sqlite3.connect(db_file)
    (blob,) = con.execute("SELECT runs FROM agno_sessions").fetchone()
    con.close()
    assert json.loads(blob)[0]["run_id"] == "legacy-run-1"


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db_file = tmp_path / "v2.db"
    _make_v2_database(db_file)
    db = SqliteDb(db_file=str(db_file))

    asyncio.run(migrate_agno_db(db))
    asyncio.run(migrate_agno_db(db))

    runs = SqliteDb(db_file=str(db_file)).get_runs(session_id="legacy-session")
    assert len(runs) == 1


def test_empty_database_does_not_need_migration(tmp_path: Path) -> None:
    db = SqliteDb(db_file=str(tmp_path / "empty.db"))
    assert asyncio.run(agno_db_needs_migration(db)) is False


def test_migration_never_calls_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "v2.db"
    _make_v2_database(db_file)
    db = SqliteDb(db_file=str(db_file))
    called: list[Any] = []
    monkeypatch.setattr(
        db, "cleanup_legacy_runs_column", lambda *a, **k: called.append((a, k)), raising=False
    )

    asyncio.run(migrate_agno_db(db))

    assert called == []


def test_ensure_tables_warns_about_v2_schema(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_file = tmp_path / "v2.db"
    _make_v2_database(db_file)
    agno_db = SqliteDb(db_file=str(db_file))
    # ``prefix="agno"`` keeps Agno's own table names (``agno_sessions``, ...).
    harness_db = AgnoHarnessDb.from_agno_db(agno_db, prefix="agno")

    with caplog.at_level(logging.WARNING, logger="agno_harness.db.classes"):
        asyncio.run(harness_db.ensure_tables())

    assert any("v2 schema" in r.getMessage() for r in caplog.records)


def test_ensure_tables_is_quiet_on_a_v3_database(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agno_db = SqliteDb(db_file=str(tmp_path / "new.db"))
    harness_db = AgnoHarnessDb.from_agno_db(agno_db)

    with caplog.at_level(logging.WARNING, logger="agno_harness.db.classes"):
        asyncio.run(harness_db.ensure_tables())

    assert not any("v2 schema" in r.getMessage() for r in caplog.records)


def test_cli_migrates_a_sqlite_file(tmp_path: Path) -> None:
    db_file = tmp_path / "v2.db"
    _make_v2_database(db_file)

    result = CliRunner().invoke(app, ["db", "migrate-agno", "--sqlite", str(db_file), "--yes"])

    assert result.exit_code == 0, result.output
    assert "2.5.6" in result.output
    assert "legacy-run-1" in result.output
    assert "never runs cleanup" in result.output


def test_cli_requires_exactly_one_target() -> None:
    result = CliRunner().invoke(app, ["db", "migrate-agno", "--yes"])
    assert result.exit_code == 2


def test_cli_stops_without_backup_confirmation(tmp_path: Path) -> None:
    db_file = tmp_path / "v2.db"
    _make_v2_database(db_file)

    result = CliRunner().invoke(app, ["db", "migrate-agno", "--sqlite", str(db_file)], input="n\n")

    assert result.exit_code == 1
    assert SqliteDb(db_file=str(db_file)).get_latest_schema_version("agno_sessions") == "2.5.6"
