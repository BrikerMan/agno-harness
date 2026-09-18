"""Pair Agno session storage with harness frame storage.

Complete history is two stores:

* Agno ``db`` — next-turn model context (``agno_sessions`` / ``agno_runs``).
* Harness ``Stores`` — the stream the user saw (frames, cards, custom events).

A durable harness log with an in-memory (or missing) Agno db is the dangerous
half-pair: the UI can replay a conversation the model will forget. That pairing
fails at runtime construction unless ``allow_ephemeral_agno_db=True``.
"""

from __future__ import annotations

from typing import Any

from ..stores.registry import Stores


class StoragePairingError(ValueError):
    """Durable harness stores were paired with an ephemeral Agno db."""


def agno_db_is_persistent(db: Any) -> bool:
    """True when ``db`` looks like a disk/server Agno backend, not a test fake."""
    if db is None:
        return False
    name = type(db).__name__
    if "InMemory" in name or name in {"FakeDb", "BrokenDb"}:
        return False
    if (
        getattr(db, "db_file", None)
        or getattr(db, "db_url", None)
        or getattr(db, "db_engine", None)
    ):
        return True
    return any(token in name for token in ("Sqlite", "Postgres", "Mysql", "Mongo", "Redis"))


def _sql_persistent_store(store: Any) -> bool:
    if store is None:
        return False
    name = type(store).__name__
    if name.startswith("InMemory"):
        return False
    if getattr(store, "is_durable", False):
        return True
    return name.startswith("SQL") or "SQLAlchemy" in name


def harness_stores_are_persistent(stores: Stores) -> bool:
    """True when frame history is on SQL, not an in-process stand-in."""
    return _sql_persistent_store(stores.event_log) or _sql_persistent_store(stores.history_archive)


def check_history_pairing(
    db: Any,
    stores: Stores,
    *,
    allow_ephemeral_agno_db: bool = False,
) -> None:
    """Refuse durable harness stores without a persistent Agno db."""
    if allow_ephemeral_agno_db or not harness_stores_are_persistent(stores):
        return
    if agno_db_is_persistent(db):
        return
    raise StoragePairingError(
        "Durable harness stores (SQL event_log / history_archive) require a "
        "persistent Agno db (SqliteDb / PostgresDb). Otherwise the UI can "
        "replay frames while the model forgets the next turn. Pass "
        "db=SqliteDb(...) on Agent and AgentRuntime, or set "
        "allow_ephemeral_agno_db=True if this pairing is intentional."
    )
