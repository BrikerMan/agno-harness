from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from agno_harness.db import (
    AgnoHarnessDb,
    AgnoHarnessPostgresDb,
    AgnoHarnessSqliteDb,
    AlembicMigrator,
    MissingHarnessTablesError,
)
from agno_harness.runtime.runtime import AgentRuntime


@pytest.mark.asyncio
async def test_sqlite_db_auto_create_creates_tables_and_stores(tmp_path: Path):
    db_file = tmp_path / "subdir" / "agent.db"
    assert not db_file.parent.exists()

    harness_db = AgnoHarnessSqliteDb(db_file=str(db_file), prefix="test_app", auto_create=True)
    assert db_file.parent.exists()

    await harness_db.ensure_tables()

    # Verify tables created
    async with harness_db.session_factory() as session:
        result = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'test_app_%'")
        )
        tables = {row[0] for row in result.fetchall()}
        assert "test_app_threads" in tables
        assert "test_app_conversation_sessions" in tables
        assert "test_app_actions" in tables
        assert "test_app_run_frames" in tables

    stores = harness_db.build_stores()
    assert stores.event_log is not None

    await harness_db.close()


@pytest.mark.asyncio
async def test_auto_create_false_raises_missing_tables(tmp_path: Path):
    db_file = tmp_path / "empty.db"
    harness_db = AgnoHarnessSqliteDb(db_file=str(db_file), prefix="test_app", auto_create=False)

    with pytest.raises(MissingHarnessTablesError) as exc_info:
        await harness_db.ensure_tables()

    msg = str(exc_info.value)
    assert "Missing 8 required harness table(s)" in msg
    assert "alembic revision --autogenerate" in msg
    assert "AlembicMigrator.declare_models(Base, prefix='test_app')" in msg

    await harness_db.close()


@pytest.mark.asyncio
async def test_alembic_version_table_detect_skips_auto_create(tmp_path: Path):
    db_file = tmp_path / "alembic.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    await engine.dispose()

    # Even with auto_create=True, presence of alembic_version prevents silent table creation
    harness_db = AgnoHarnessSqliteDb(db_file=str(db_file), prefix="test_app", auto_create=True)
    with pytest.raises(MissingHarnessTablesError) as exc_info:
        await harness_db.ensure_tables()

    assert "alembic_version" in str(exc_info.value)
    await harness_db.close()


@pytest.mark.asyncio
async def test_from_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    harness_db = AgnoHarnessDb.from_session_factory(
        session_factory=session_factory,
        prefix="shared",
        auto_create=True,
    )
    assert harness_db.prefix == "shared"
    assert harness_db._owns_engine is False

    await harness_db.ensure_tables()

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'shared_%'")
        )
        tables = {row[0] for row in result.fetchall()}
        assert "shared_conversation_sessions" in tables

    # close() should not dispose external engine
    await harness_db.close()
    async with session_factory() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1

    await engine.dispose()


def test_alembic_migrator_declare_models():
    class CustomBase(DeclarativeBase):
        pass

    assert len(CustomBase.metadata.tables) == 0
    models = AlembicMigrator.declare_models(base=CustomBase, prefix="admin_agent")
    assert len(models) == 8
    assert "admin_agent_threads" in CustomBase.metadata.tables
    assert "admin_agent_conversation_sessions" in CustomBase.metadata.tables
    assert "admin_agent_run_frames" in CustomBase.metadata.tables


def test_postgres_url_driver_normalization():
    with pytest.MonkeyPatch.context() as mp:
        mock_create = MagicMock()
        mp.setattr("agno_harness.db.classes.create_async_engine", mock_create)
        db = AgnoHarnessPostgresDb(
            db_url="postgresql://user:pass@localhost:5432/testdb", prefix="pg"
        )
        assert db.db_url == "postgresql+asyncpg://user:pass@localhost:5432/testdb"
        mock_create.assert_called_once_with("postgresql+asyncpg://user:pass@localhost:5432/testdb")


@pytest.mark.asyncio
async def test_runtime_auto_binds_harness_db(tmp_path: Path):
    db_file = tmp_path / "runtime.db"
    harness_db = AgnoHarnessSqliteDb(db_file=str(db_file), prefix="rt_app", auto_create=True)

    agent_mock = MagicMock()
    agent_mock.name = "test-agent"
    agent_mock.db = MagicMock()
    agent_mock.db.session_table_name = "sessions"

    runtime = AgentRuntime(
        agent=agent_mock,
        harness_db=harness_db,
    )

    assert runtime.harness_db is harness_db
    # agent.db session_table_name prefix aligned
    assert agent_mock.db.session_table_name == "rt_app_sessions"
    assert runtime.stores is not None
    assert runtime.stores.event_log is not None

    await harness_db.close()
