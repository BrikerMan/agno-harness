"""Registerable persistence, against SQLite and (when available) PostgreSQL."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agno_relay.stores import (
    CustomEventMixin,
    CustomEventStore,
    HistoryArchive,
    InMemoryCustomEventStore,
    RunArchiveMixin,
    RunFrameMixin,
    RunRecordMixin,
    StoreRegistry,
)


class Base(DeclarativeBase):
    """The application's own declarative base -- the toolbox never owns one."""


class AppCustomEvent(Base, CustomEventMixin):
    __tablename__ = "app_custom_events"
    # An extra column the toolbox knows nothing about, which must not break it.
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")


class AppRunArchive(Base, RunArchiveMixin):
    __tablename__ = "app_run_archives"


class AppRunFrame(Base, RunFrameMixin):
    __tablename__ = "app_run_frames"


class AppRunRecord(Base, RunRecordMixin):
    __tablename__ = "app_run_records"


DATABASE_URLS = [("sqlite", "sqlite+aiosqlite:///:memory:")]
if os.getenv("TEST_POSTGRES_URL"):
    DATABASE_URLS.append(("postgres", os.environ["TEST_POSTGRES_URL"]))


@pytest.fixture(params=DATABASE_URLS, ids=[name for name, _ in DATABASE_URLS])
async def session_factory(request):
    name, url = request.param
    if name == "postgres":
        pytest.importorskip("asyncpg")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def stores(session_factory):
    return (
        StoreRegistry()
        .register(CustomEventStore, AppCustomEvent)
        .register(HistoryArchive, AppRunArchive)
        .register_event_log(AppRunFrame, AppRunRecord)
        .build(session_factory)
    )


class TestCustomEvents:
    async def test_saved_events_come_back_grouped_by_thread(self, stores):
        await stores.custom_events.save("t1", "r1", "billing", {"cost": 3})
        await stores.custom_events.save("t2", "r9", "billing", {"cost": 9})
        rows = await stores.custom_events.list_by_thread("t1")
        assert len(rows) == 1
        assert rows[0]["name"] == "billing"
        assert rows[0]["value"] == {"cost": 3}
        assert rows[0]["runId"] == "r1"

    async def test_insertion_order_is_preserved(self, stores):
        for index in range(5):
            await stores.custom_events.save("t1", "r1", f"e{index}", {"i": index})
        rows = await stores.custom_events.list_by_thread("t1")
        assert [row["value"]["i"] for row in rows] == [0, 1, 2, 3, 4]

    async def test_deleting_a_thread_removes_only_its_rows(self, stores):
        await stores.custom_events.save("t1", "r1", "a", {})
        await stores.custom_events.save("t2", "r2", "b", {})
        assert await stores.custom_events.delete_by_thread("t1") == 1
        assert await stores.custom_events.list_by_thread("t1") == []
        assert len(await stores.custom_events.list_by_thread("t2")) == 1

    async def test_non_ascii_values_round_trip(self, stores):
        await stores.custom_events.save("t1", "r1", "note", {"text": "搜索结果"})
        rows = await stores.custom_events.list_by_thread("t1")
        assert rows[0]["value"]["text"] == "搜索结果"


class TestHistoryArchive:
    async def test_a_run_round_trips_as_coalesced_events(self, stores):
        await stores.history_archive.save_run(
            "t1",
            "r1",
            [{"type": "TEXT_MESSAGE_CONTENT", "delta": "Hello"}],
            user_id="alice",
        )
        runs = await stores.history_archive.list_thread("t1", user_id="alice")
        assert runs == [("r1", [{"type": "TEXT_MESSAGE_CONTENT", "delta": "Hello"}])]

    async def test_saving_again_replaces_the_row(self, stores):
        await stores.history_archive.save_run("t1", "r1", [{"n": 1}])
        await stores.history_archive.save_run("t1", "r1", [{"n": 2}])
        runs = await stores.history_archive.list_thread("t1")
        assert runs == [("r1", [{"n": 2}])]

    async def test_a_history_archive_counts_as_durable(self, stores):
        assert stores.history_archive is not None
        assert stores.is_durable is True


class TestEventLog:
    async def test_the_registry_builds_a_working_log(self, stores):
        """Registered as two models because the log spans two tables: the frames
        and the per-run status that makes them findable."""
        await stores.event_log.start_run("r1", "t1", user_id="alice")
        await stores.event_log.append("r1", [{"type": "RUN_STARTED"}])

        frames = await stores.event_log.read("r1")
        assert [f.event["type"] for f in frames] == ["RUN_STARTED"]
        assert (await stores.event_log.get_run("r1")).user_id == "alice"

    async def test_a_sql_log_cannot_tail(self, stores):
        """Which is the documented "no Redis means history only" degradation."""
        assert stores.event_stream is None
        assert stores.resume_mode.value == "history"


class TestRegistry:
    def test_an_event_log_model_missing_columns_is_rejected_too(self):
        class IncompleteFrame(Base):
            __tablename__ = "incomplete_frames"
            id: Mapped[int] = mapped_column(primary_key=True)

        with pytest.raises(TypeError, match="event_json"):
            StoreRegistry().register_event_log(IncompleteFrame, AppRunRecord)

    def test_a_model_missing_required_columns_is_rejected_at_registration(self):
        class Incomplete(Base):
            __tablename__ = "incomplete"
            id: Mapped[int] = mapped_column(primary_key=True)

        with pytest.raises(TypeError) as excinfo:
            StoreRegistry().register(CustomEventStore, Incomplete)
        assert "missing column" in str(excinfo.value)
        assert "value_json" in str(excinfo.value)

    def test_an_unknown_store_kind_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown store kind"):
            StoreRegistry().register(str, AppCustomEvent)

    def test_registering_a_model_without_a_session_factory_fails_loudly(self):
        registry = StoreRegistry().register(CustomEventStore, AppCustomEvent)
        with pytest.raises(ValueError, match="session_factory"):
            registry.build()

    def test_a_custom_store_instance_bypasses_sqlalchemy(self):
        store = InMemoryCustomEventStore()
        stores = StoreRegistry().register_instance(CustomEventStore, store).build()
        assert stores.custom_events is store

    def test_an_empty_registry_builds_an_empty_store_set(self):
        stores = StoreRegistry().build()
        assert stores.custom_events is None
        assert stores.event_log is None


class TestInMemoryStore:
    async def test_it_satisfies_the_protocol(self):
        store = InMemoryCustomEventStore()
        assert isinstance(store, CustomEventStore)
        await store.save("t1", "r1", "n", {"v": 1})
        assert (await store.list_by_thread("t1"))[0]["value"] == {"v": 1}
        assert await store.delete_by_thread("t1") == 1
