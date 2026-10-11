"""Move an Agno v2 database to the v3 schema.

Agno v3 stores each run in its own table (``agno_runs``) instead of a JSON blob
on the session row, and adds ``user_id`` columns elsewhere. Agno ships the
migration (``MigrationManager``); this module wraps it for harness projects:
one call, a report you can read, and nothing destructive.

It never calls Agno's ``cleanup_legacy_runs_*`` helpers and never passes
``force=True``. The legacy ``runs`` column stays as a backup until you have
compared the old and new history and hold a restorable backup yourself.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

#: First Agno schema version that stores runs in their own table.
RUNS_TABLE_SCHEMA_VERSION = Version("3.0.0")


@dataclass
class AgnoMigrationReport:
    """What :func:`migrate_agno_db` saw and did."""

    sessions_table: str
    runs_table: str
    version_before: str | None = None
    version_after: str | None = None
    sample_run_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def migrated(self) -> bool:
        return _is_v3(self.version_after)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _is_v3(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        return Version(str(raw)) >= RUNS_TABLE_SCHEMA_VERSION
    except InvalidVersion:
        return False


async def _schema_version(db: Any) -> str | None:
    getter = getattr(db, "get_latest_schema_version", None)
    table = getattr(db, "session_table_name", None)
    if not callable(getter) or not table:
        return None
    try:
        raw = await _maybe_await(getter(table))
    except Exception as exc:  # noqa: BLE001 - table may not exist yet
        logger.debug("could not read schema version for %s: %s", table, exc)
        return None
    return str(raw) if raw else None


async def agno_db_needs_migration(db: Any) -> bool:
    """True when ``db`` holds an Agno schema older than v3.

    A database with no version stamp (empty, or a backend that does not stamp)
    is reported as not needing migration; there is nothing to move.
    """
    table = getattr(db, "session_table_name", None)
    exists = getattr(db, "table_exists", None)
    if table and callable(exists):
        try:
            if not await _maybe_await(exists(table)):
                return False  # nothing stored yet; v3 creates the new layout
        except Exception as exc:  # noqa: BLE001 - backend cannot tell; fall through
            logger.debug("could not check for table %s: %s", table, exc)
    raw = await _schema_version(db)
    if raw is None:
        return False
    return not _is_v3(raw)


async def migrate_agno_db(db: Any, *, sample: int = 5) -> AgnoMigrationReport:
    """Run Agno's v3 migrations on ``db`` and report the result.

    Take a restorable backup and stop every writer first. The migration is
    non-destructive and safe to re-run, but a sample of runs is not proof that
    every legacy run was copied; compare the full history before any cleanup.
    """
    from agno.db.migrations.manager import MigrationManager

    report = AgnoMigrationReport(
        sessions_table=str(getattr(db, "session_table_name", "") or ""),
        runs_table=str(getattr(db, "runs_table_name", "") or ""),
    )
    report.version_before = await _schema_version(db)

    await MigrationManager(db).up()

    report.version_after = await _schema_version(db)
    get_runs = getattr(db, "get_runs", None)
    if callable(get_runs):
        runs = await _maybe_await(get_runs(limit=sample))
        if isinstance(runs, tuple):
            runs = runs[0]
        report.sample_run_ids = [
            str(r.get("run_id") if isinstance(r, dict) else getattr(r, "run_id", ""))
            for r in (runs or [])
        ]
        if not report.sample_run_ids:
            report.notes.append(
                "No runs found in the sample. An empty database may legitimately have none."
            )
    report.notes.append(
        "Legacy session history was kept. Compare every legacy session and run with "
        f"'{report.runs_table}' and keep a restorable backup before any cleanup."
    )
    report.notes.append("agno-harness never runs cleanup_legacy_runs_* or passes force=True.")
    return report


__all__ = [
    "RUNS_TABLE_SCHEMA_VERSION",
    "AgnoMigrationReport",
    "agno_db_needs_migration",
    "migrate_agno_db",
]
