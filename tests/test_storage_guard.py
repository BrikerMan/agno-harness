import pytest

from agno_harness.runtime.storage_guard import (
    StoragePairingError,
    agno_db_is_persistent,
    check_history_pairing,
    harness_stores_are_persistent,
)
from agno_harness.stores import InMemoryHistoryArchive, InMemoryRunEventLog, Stores
from agno_harness.stores.sql_log import SQLRunEventLog

from .conftest import FakeAgent


class _FileBackedDb:
    db_file = "sessions.db"


def _sql_log():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase

    from agno_harness.stores import RunFrameMixin, RunRecordMixin

    class Base(DeclarativeBase):
        pass

    class Frame(Base, RunFrameMixin):
        __tablename__ = "guard_frames"

    class Record(Base, RunRecordMixin):
        __tablename__ = "guard_records"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return SQLRunEventLog(async_sessionmaker(engine, expire_on_commit=False), Frame, Record)


def test_empty_pair_is_allowed():
    check_history_pairing(None, Stores())


def test_in_memory_harness_does_not_require_agno_db():
    from .test_longrun import _HistoryOnlyLog

    check_history_pairing(None, Stores(event_log=InMemoryRunEventLog()))
    check_history_pairing(
        None,
        Stores(event_log=InMemoryRunEventLog(), history_archive=InMemoryHistoryArchive()),
    )
    check_history_pairing(None, Stores(event_log=_HistoryOnlyLog()))


def test_sql_harness_without_agno_db_fails():
    stores = Stores(event_log=_sql_log())
    assert harness_stores_are_persistent(stores)
    assert not agno_db_is_persistent(None)
    with pytest.raises(StoragePairingError, match="persistent Agno db"):
        check_history_pairing(None, stores)


def test_sql_harness_with_file_backed_agno_db_is_ok():
    check_history_pairing(_FileBackedDb(), Stores(event_log=_sql_log()))


def test_sql_harness_opt_in_allows_ephemeral_agno_db():
    check_history_pairing(
        None,
        Stores(event_log=_sql_log()),
        allow_ephemeral_agno_db=True,
    )


def test_runtime_refuses_sql_stores_without_agno_db():
    from agno_harness import AgentRuntime

    with pytest.raises(StoragePairingError):
        AgentRuntime(agent=FakeAgent([]), stores=Stores(event_log=_sql_log()))


def test_runtime_allows_explicit_ephemeral_agno_db():
    from agno_harness import AgentRuntime

    runtime = AgentRuntime(
        agent=FakeAgent([]),
        stores=Stores(event_log=_sql_log()),
        allow_ephemeral_agno_db=True,
    )
    assert runtime.db is None
