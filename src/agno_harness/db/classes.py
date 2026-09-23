from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..stores.prefix import apply_table_prefix, resolve_prefix
from ..stores.registry import StoreRegistry, Stores
from ..stores.sql_models import DefaultRelayBase, register_harness_models
from ..stores.stores import CustomEventStore, HistoryArchive
from ..stores.thread_store import BaseThreadStore

logger = logging.getLogger(__name__)


class MissingHarnessTablesError(RuntimeError):
    """Raised when required agno-harness persistence tables are missing."""

    def __init__(self, missing: Sequence[str], prefix: str | None = None) -> None:
        self.missing = list(missing)
        self.prefix = prefix
        prefix_str = f"'{prefix}'" if prefix else "default"
        msg = (
            f"\n[agno-harness] Missing {len(missing)} required harness table(s) for prefix {prefix_str}:\n"
            + "\n".join(f"  - {t}" for t in self.missing)
            + "\n\nQuick Fix:\n"
            + "  1. Add to your models/__init__.py:\n"
            + "     from agno_harness.db import AlembicMigrator\n"
            + "     AlembicMigrator.declare_models(Base"
            + (f", prefix={prefix!r}" if prefix else "")
            + ")\n"
            + "  2. Run migration commands:\n"
            + '     alembic revision --autogenerate -m "add harness tables"\n'
            + "     alembic upgrade head\n"
            + "  (Or set auto_create=True on AgnoHarnessDb without an existing alembic_version table for automatic local creation)\n"
        )
        super().__init__(msg)


class AgnoHarnessDb:
    """Base class for agno-harness database adapters."""

    def __init__(
        self,
        *,
        engine: AsyncEngine | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        prefix: str | None = None,
        auto_create: bool = True,
        owns_engine: bool = False,
    ) -> None:
        self.prefix = resolve_prefix(prefix)
        self.auto_create = auto_create
        self._owns_engine = owns_engine
        self._tables_ensured = False

        if session_factory is not None:
            self.session_factory = session_factory
            self.engine = engine or getattr(session_factory, "kw", {}).get("bind")
        elif engine is not None:
            self.engine = engine
            self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        else:
            raise ValueError("Either 'engine' or 'session_factory' must be provided.")

        self.models = register_harness_models(prefix=self.prefix)

    @property
    def table_names(self) -> list[str]:
        """Names of the 7 persistence tables for this harness db prefix."""
        return [model.__tablename__ for model in self.models.values()]

    async def ensure_tables(self) -> None:
        """Verify harness tables exist; create if auto_create=True and no Alembic detected."""
        if self._tables_ensured:
            return

        if self.engine is None:
            async with self.session_factory() as session:
                bind = session.bind
                if isinstance(bind, AsyncEngine):
                    self.engine = bind

        if self.engine is None:
            logger.debug(
                "[agno-harness] No AsyncEngine available to inspect tables; skipping check."
            )
            self._tables_ensured = True
            return

        async with self.engine.connect() as conn:
            existing = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )

            missing = [t for t in self.table_names if t not in existing]
            if not missing:
                self._tables_ensured = True
                return

            if "alembic_version" in existing:
                logger.warning(
                    f"[agno-harness] Detected Alembic-managed database ('alembic_version' table exists). "
                    f"Skipping auto-create for missing tables: {missing}. "
                    "Please run 'alembic revision --autogenerate' and 'alembic upgrade head' "
                    f"after AlembicMigrator.declare_models(Base, prefix={self.prefix!r})."
                )
                raise MissingHarnessTablesError(missing, prefix=self.prefix)

            if not self.auto_create:
                raise MissingHarnessTablesError(missing, prefix=self.prefix)

            logger.warning(
                f"[agno-harness] ⚠️ Initialized harness tables automatically for prefix '{self.prefix}'. "
                "For production environments, it is recommended to manage schema versions via AlembicMigrator.declare_models(Base)."
            )
            tables_to_create = [model.__table__ for model in self.models.values()]
            await conn.run_sync(
                lambda sync_conn: DefaultRelayBase.metadata.create_all(
                    sync_conn, tables=tables_to_create
                )
            )

        self._tables_ensured = True

    def build_stores(self, event_log: Any = None, event_stream: Any = None) -> Stores:
        """Build standard Stores wired to this harness db's models and session factory."""
        registry = (
            StoreRegistry()
            .register(CustomEventStore, self.models["custom_events"])
            .register(HistoryArchive, self.models["run_archives"])
            .register(BaseThreadStore, self.models["threads"])
        )
        if event_log is None:
            registry.register_event_log(self.models["run_frames"], self.models["run_records"])

        built = registry.build(self.session_factory)
        return Stores(
            custom_events=built.custom_events,
            history_archive=built.history_archive,
            event_log=event_log or built.event_log,
            event_stream=event_stream or event_log or built.event_stream,
            threads=built.threads,
        )

    async def close(self) -> None:
        """Dispose of underlying AsyncEngine if owned."""
        if self._owns_engine and self.engine is not None:
            await self.engine.dispose()

    @classmethod
    def from_session_factory(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        prefix: str | None = None,
        auto_create: bool = True,
    ) -> AgnoHarnessDb:
        """Create a harness db sharing an existing async session factory."""
        return cls(
            session_factory=session_factory,
            prefix=prefix,
            auto_create=auto_create,
            owns_engine=False,
        )

    @classmethod
    def from_agno_db(
        cls,
        agno_db: Any,
        *,
        prefix: str | None = None,
        auto_create: bool = True,
    ) -> AgnoHarnessDb:
        """Derive a harness db instance from an existing Agno database object."""
        if agno_db is None:
            raise ValueError("agno_db cannot be None")

        # Synchronize Agno table prefix
        apply_table_prefix(agno_db, prefix=prefix)

        db_file = getattr(agno_db, "db_file", None)
        db_url = getattr(agno_db, "db_url", None)
        async_factory = getattr(agno_db, "async_session_factory", None)

        if async_factory is not None:
            return cls.from_session_factory(
                session_factory=async_factory,
                prefix=prefix,
                auto_create=auto_create,
            )

        if db_file:
            return AgnoHarnessSqliteDb(
                db_file=str(db_file),
                prefix=prefix,
                auto_create=auto_create,
            )

        url_str = str(db_url) if db_url else ""
        if "sqlite" in url_str:
            return AgnoHarnessSqliteDb(
                db_url=url_str,
                prefix=prefix,
                auto_create=auto_create,
            )
        if "postgres" in url_str:
            return AgnoHarnessPostgresDb(
                db_url=url_str,
                prefix=prefix,
                auto_create=auto_create,
            )

        return AgnoHarnessSqliteDb(
            prefix=prefix,
            auto_create=auto_create,
        )


class AgnoHarnessSqliteDb(AgnoHarnessDb):
    """SQLite implementation of AgnoHarnessDb using aiosqlite."""

    def __init__(
        self,
        db_file: str | Path | None = None,
        *,
        db_url: str | None = None,
        prefix: str | None = None,
        auto_create: bool = True,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if session_factory is not None:
            super().__init__(
                session_factory=session_factory,
                prefix=prefix,
                auto_create=auto_create,
                owns_engine=False,
            )
            return

        final_url: str
        if db_file is not None:
            file_str = str(db_file)
            if file_str != ":memory:":
                Path(file_str).parent.mkdir(parents=True, exist_ok=True)
                final_url = f"sqlite+aiosqlite:///{Path(file_str).resolve()}"
            else:
                final_url = "sqlite+aiosqlite:///:memory:"
        elif db_url is not None:
            final_url = db_url
            if final_url.startswith("sqlite://") and not final_url.startswith(
                "sqlite+aiosqlite://"
            ):
                final_url = "sqlite+aiosqlite://" + final_url[len("sqlite://") :]
        else:
            default_path = Path("agno_harness.db")
            default_path.parent.mkdir(parents=True, exist_ok=True)
            final_url = f"sqlite+aiosqlite:///{default_path.resolve()}"

        engine = create_async_engine(final_url)
        super().__init__(
            engine=engine,
            prefix=prefix,
            auto_create=auto_create,
            owns_engine=True,
        )


class AgnoHarnessPostgresDb(AgnoHarnessDb):
    """PostgreSQL implementation of AgnoHarnessDb using asyncpg/psycopg_async."""

    def __init__(
        self,
        db_url: str | None = None,
        *,
        prefix: str | None = None,
        auto_create: bool = True,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if session_factory is not None:
            super().__init__(
                session_factory=session_factory,
                prefix=prefix,
                auto_create=auto_create,
                owns_engine=False,
            )
            return

        if not db_url:
            raise ValueError("Postgres requires a valid 'db_url' or 'session_factory'.")

        final_url = db_url
        if final_url.startswith("postgresql://"):
            final_url = "postgresql+asyncpg://" + final_url[len("postgresql://") :]
        elif final_url.startswith("postgres://"):
            final_url = "postgresql+asyncpg://" + final_url[len("postgres://") :]

        self.db_url = final_url
        engine = create_async_engine(final_url)
        super().__init__(
            engine=engine,
            prefix=prefix,
            auto_create=auto_create,
            owns_engine=True,
        )


__all__ = [
    "AgnoHarnessDb",
    "AgnoHarnessPostgresDb",
    "AgnoHarnessSqliteDb",
    "MissingHarnessTablesError",
]
