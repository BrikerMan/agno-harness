"""The event log, with every implementation held to the same contract.

The point of parametrising is that the three backends have wildly different
mechanics — a dict, a table with a sequence column, a Redis stream — and any
divergence in what an offset means or what ``read(after=)`` returns would show
up as "resume works in tests, drops events in production". So the cases are
written once and run against all of them.

Redis is opt-in via ``TEST_REDIS_URL``, matching how Postgres is handled in
``test_persistence.py``; ``make test-fast`` deselects the marker.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from agno_relay.core.log import Frame, FrameKind, RunStatus, coalesce_events, select_frames
from agno_relay.stores import (
    InMemoryRunEventLog,
    ResumeMode,
    RunFrameMixin,
    RunRecordMixin,
    SQLRunEventLog,
    Stores,
)


class Base(DeclarativeBase):
    pass


class AppRunFrame(Base, RunFrameMixin):
    __tablename__ = "app_run_frames"


class AppRunRecord(Base, RunRecordMixin):
    __tablename__ = "app_run_records"


REDIS_URL = os.getenv("TEST_REDIS_URL")

BACKENDS = ["memory", "sql"]
if REDIS_URL:
    BACKENDS.append(pytest.param("redis", marks=pytest.mark.redis))


@pytest.fixture(params=BACKENDS)
async def log(request):
    """One log per test, whichever backend, cleaned up after."""
    if request.param == "memory":
        yield InMemoryRunEventLog()
        return

    if request.param == "sql":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield SQLRunEventLog(
            async_sessionmaker(engine, expire_on_commit=False), AppRunFrame, AppRunRecord
        )
        await engine.dispose()
        return

    pytest.importorskip("redis")
    from agno_relay.stores.redis_log import RedisRunEventLog

    backend = RedisRunEventLog.from_url(REDIS_URL, namespace=f"test-{os.getpid()}")
    yield backend
    await backend.delete_thread("t1")
    await backend.delete_thread("t2")
    await backend.client.aclose()


def event(index: int) -> dict:
    return {"type": "TEXT_MESSAGE_CONTENT", "delta": f"chunk-{index}"}


def deltas(frames: list[Frame]) -> list[str]:
    return [frame.event["delta"] for frame in frames]


class TestAppendAndRead:
    async def test_frames_come_back_in_the_order_they_went_in(self, log):
        await log.start_run("r1", "t1")
        await log.append("r1", [event(0), event(1)])
        await log.append("r1", [event(2)])

        assert deltas(await log.read("r1")) == ["chunk-0", "chunk-1", "chunk-2"]

    async def test_reading_after_an_offset_excludes_that_frame(self, log):
        """The resume contract: ``after`` is the last frame the client *has*.

        Off-by-one here is the difference between a clean reconnect and a
        duplicated or dropped token, and it is invisible until someone's network
        drops mid-sentence.
        """
        await log.start_run("r1", "t1")
        await log.append("r1", [event(i) for i in range(4)])

        all_frames = await log.read("r1")
        resumed = await log.read("r1", after=all_frames[1].offset)

        assert deltas(resumed) == ["chunk-2", "chunk-3"]

    async def test_a_prefix_plus_its_remainder_is_the_whole_run(self, log):
        await log.start_run("r1", "t1")
        await log.append("r1", [event(i) for i in range(6)])

        whole = await log.read("r1")
        cut = whole[2].offset
        assert deltas(whole[:3]) + deltas(await log.read("r1", after=cut)) == deltas(whole)

    async def test_offsets_are_unique_and_increasing(self, log):
        await log.start_run("r1", "t1")
        await log.append("r1", [event(i) for i in range(5)])

        offsets = [frame.offset for frame in await log.read("r1")]
        assert len(set(offsets)) == len(offsets)
        assert offsets == sorted(offsets, key=_sortable)

    async def test_an_unknown_run_reads_empty_rather_than_raising(self, log):
        assert await log.read("never-existed") == []

    async def test_appending_nothing_is_allowed(self, log):
        await log.start_run("r1", "t1")
        await log.append("r1", [])
        assert await log.read("r1") == []


class TestRunRecords:
    async def test_a_started_run_is_running_and_owned(self, log):
        await log.start_run("r1", "t1", user_id="alice")

        record = await log.get_run("r1")
        assert record is not None
        assert (record.thread_id, record.user_id, record.status) == (
            "t1",
            "alice",
            RunStatus.RUNNING,
        )

    async def test_starting_the_same_run_twice_does_not_restart_it(self, log):
        """Idempotence is what lets ``attach`` be safe to call on every reload."""
        first = await log.start_run("r1", "t1", user_id="alice")
        await log.append("r1", [event(0)])
        again = await log.start_run("r1", "t1", user_id="alice")

        assert again.started_at == first.started_at
        assert deltas(await log.read("r1")) == ["chunk-0"]

    async def test_starting_a_closed_run_reopens_it(self, log):
        """A finished id must not keep the old frames or stay FINISHED."""
        await log.start_run("r1", "t1")
        await log.append("r1", [event(0)])
        await log.set_status("r1", RunStatus.FINISHED)

        again = await log.start_run("r1", "t1")

        assert again.status is RunStatus.RUNNING
        assert again.is_producing
        assert await log.read("r1") == []

    async def test_status_and_error_survive(self, log):
        await log.start_run("r1", "t1")
        await log.set_status("r1", RunStatus.ERROR, error="model exploded")

        record = await log.get_run("r1")
        assert record.status is RunStatus.ERROR
        assert record.error == "model exploded"
        assert not record.is_open

    async def test_a_run_can_be_marked_unrecordable(self, log):
        """Set when the log itself failed mid-run, so the client is told."""
        await log.start_run("r1", "t1")
        await log.set_status("r1", RunStatus.RUNNING, unrecordable=True)

        assert (await log.get_run("r1")).unrecordable is True

    async def test_a_paused_run_is_still_open(self, log):
        """HITL: waiting on a human is not the same as finished.

        A paused run that reported itself closed would vanish from the active
        list on reload, while Agno still holds the session locked awaiting an
        answer nobody can now give.
        """
        await log.start_run("r1", "t1")
        await log.set_status("r1", RunStatus.PAUSED)

        assert (await log.get_run("r1")).is_open is True

    async def test_runs_are_listed_per_thread(self, log):
        await log.start_run("r1", "t1")
        await log.start_run("r2", "t1")
        await log.start_run("r3", "t2")

        assert {r.run_id for r in await log.list_runs("t1")} == {"r1", "r2"}

    async def test_listing_by_user_hides_other_peoples_runs(self, log):
        await log.start_run("r1", "t1", user_id="alice")
        await log.start_run("r2", "t1", user_id="bob")

        assert [r.run_id for r in await log.list_runs("t1", user_id="alice")] == ["r1"]

    async def test_deleting_a_thread_takes_its_frames_with_it(self, log):
        await log.start_run("r1", "t1")
        await log.append("r1", [event(0)])

        assert await log.delete_thread("t1") == 1
        assert await log.read("r1") == []
        assert await log.get_run("r1") is None

    async def test_an_unknown_run_has_no_record(self, log):
        assert await log.get_run("nope") is None


class TestLiveTail:
    """Only the backends that claim to stream."""

    @pytest.fixture
    def stream(self, log):
        if not hasattr(log, "tail"):
            pytest.skip(f"{type(log).__name__} is history-only by design")
        return log

    async def test_a_tail_delivers_frames_written_after_it_started(self, stream):
        await stream.start_run("r1", "t1")

        received: list[str] = []

        async def consume() -> None:
            async for frame in stream.tail("r1"):
                received.append(frame.event["delta"])

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await stream.append("r1", [event(0), event(1)])
        await asyncio.sleep(0.05)
        await stream.set_status("r1", RunStatus.FINISHED)
        await asyncio.wait_for(task, timeout=5)

        assert received == ["chunk-0", "chunk-1"]

    async def test_a_tail_replays_what_it_missed_before_following(self, stream):
        """A reconnecting client must not have to choose between the two."""
        await stream.start_run("r1", "t1")
        await stream.append("r1", [event(0)])

        received: list[str] = []

        async def consume() -> None:
            async for frame in stream.tail("r1"):
                received.append(frame.event["delta"])

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await stream.append("r1", [event(1)])
        await asyncio.sleep(0.05)
        await stream.set_status("r1", RunStatus.FINISHED)
        await asyncio.wait_for(task, timeout=5)

        assert received == ["chunk-0", "chunk-1"]

    async def test_a_tail_of_a_finished_run_ends_instead_of_hanging(self, stream):
        await stream.start_run("r1", "t1")
        await stream.append("r1", [event(0)])
        await stream.set_status("r1", RunStatus.FINISHED)

        frames = [frame async for frame in stream.tail("r1")]

        assert deltas(frames) == ["chunk-0"]

    async def test_a_tail_of_a_paused_run_ends_instead_of_hanging(self, stream):
        """HITL: paused is still open, but nothing more will be written."""
        await stream.start_run("r1", "t1")
        await stream.append("r1", [event(0)])
        await stream.set_status("r1", RunStatus.PAUSED)

        async def _all() -> list:
            return [frame async for frame in stream.tail("r1")]

        frames = await asyncio.wait_for(_all(), timeout=2)

        assert deltas(frames) == ["chunk-0"]

    async def test_a_tail_resumes_from_an_offset(self, stream):
        await stream.start_run("r1", "t1")
        await stream.append("r1", [event(0), event(1)])
        first = (await stream.read("r1"))[0]
        await stream.set_status("r1", RunStatus.FINISHED)

        frames = [frame async for frame in stream.tail("r1", after=first.offset)]

        assert deltas(frames) == ["chunk-1"]


@pytest.mark.redis
@pytest.mark.skipif(not REDIS_URL, reason="needs TEST_REDIS_URL")
class TestOrphanDetection:
    """A run whose process died must stop claiming to be running.

    This is Redis-specific because it is the only backend with an expiring key
    to hang it on. Without it, killing the server mid-run leaves a record that
    says ``running`` forever, and every client that reconnects waits politely
    for frames from a process that no longer exists.
    """

    @pytest.fixture
    async def log(self):
        pytest.importorskip("redis")
        from agno_relay.stores.redis_log import RedisRunEventLog

        backend = RedisRunEventLog.from_url(
            REDIS_URL, namespace=f"orphan-{os.getpid()}", heartbeat_ttl=1
        )
        yield backend
        await backend.delete_thread("t1")
        await backend.client.aclose()

    async def test_a_run_whose_heartbeat_lapsed_reads_as_failed(self, log):
        await log.start_run("r1", "t1")
        assert (await log.get_run("r1")).status is RunStatus.RUNNING

        await asyncio.sleep(1.2)

        record = await log.get_run("r1")
        assert record.status is RunStatus.ERROR
        assert "stopped responding" in record.error

    async def test_a_heartbeat_keeps_a_quiet_run_alive(self, log):
        """A run can be slow without being dead — a long tool call, say."""
        await log.start_run("r1", "t1")
        await asyncio.sleep(0.6)
        await log.heartbeat("r1")
        await asyncio.sleep(0.6)

        assert (await log.get_run("r1")).status is RunStatus.RUNNING

    async def test_a_finished_run_is_not_mistaken_for_an_orphan(self, log):
        await log.start_run("r1", "t1")
        await log.set_status("r1", RunStatus.FINISHED)
        await asyncio.sleep(1.2)

        assert (await log.get_run("r1")).status is RunStatus.FINISHED

    async def test_a_paused_run_is_exempt_from_the_heartbeat(self, log):
        """Nothing is driving a paused run, so the missing beat proves nothing.

        Failing it would delete the one thing a returning user needs: the run
        that is waiting on their answer.
        """
        await log.start_run("r1", "t1")
        await log.set_status("r1", RunStatus.PAUSED)
        await asyncio.sleep(1.2)

        assert (await log.get_run("r1")).status is RunStatus.PAUSED


class TestCompaction:
    """Snapshots are not written yet, but the read path is already final.

    Storing ``kind`` from day one is what makes turning compaction on later a
    configuration change rather than a data migration.
    """

    def test_deltas_are_returned_when_there_is_no_snapshot(self):
        frames = [Frame("1", event(0)), Frame("2", event(1))]
        assert select_frames(frames) == frames

    def test_a_snapshot_supersedes_the_deltas_it_replaced(self):
        frames = [
            Frame("1", event(0)),
            Frame("2", event(1)),
            Frame("3", {"type": "SNAPSHOT"}, kind=FrameKind.SNAPSHOT),
        ]
        assert [f.event for f in select_frames(frames)] == [{"type": "SNAPSHOT"}]

    def test_deltas_after_a_snapshot_still_arrive(self):
        """Otherwise compacting a live run would truncate it as it ran."""
        frames = [
            Frame("1", event(0)),
            Frame("2", {"type": "SNAPSHOT"}, kind=FrameKind.SNAPSHOT),
            Frame("3", event(9)),
        ]
        assert [f.event for f in select_frames(frames)] == [{"type": "SNAPSHOT"}, event(9)]


class TestCoalesce:
    def test_consecutive_content_deltas_become_one_event(self):
        events = [
            {"type": "TEXT_MESSAGE_START", "messageId": "m1"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Hel"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "lo"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
        ]
        merged = coalesce_events(events)
        assert [e["type"] for e in merged] == [
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
        ]
        assert merged[1]["delta"] == "Hello"

    def test_different_messages_stay_apart(self):
        events = [
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a", "delta": "1"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "b", "delta": "2"},
        ]
        assert len(coalesce_events(events)) == 2


class TestResumeCapability:
    """What the server promises a client, as one value rather than a guess."""

    def test_no_log_means_no_resume(self):
        assert Stores().resume_mode is ResumeMode.NONE

    def test_a_log_that_cannot_tail_offers_history_only(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        log = SQLRunEventLog(async_sessionmaker(engine), AppRunFrame, AppRunRecord)
        assert Stores(event_log=log).resume_mode is ResumeMode.HISTORY

    def test_a_log_that_can_tail_is_its_own_stream(self):
        stores = Stores(event_log=InMemoryRunEventLog())
        assert stores.event_stream is stores.event_log
        assert stores.resume_mode is ResumeMode.LIVE

    def test_a_hot_stream_can_be_paired_with_a_durable_log(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        stores = Stores(
            event_log=SQLRunEventLog(async_sessionmaker(engine), AppRunFrame, AppRunRecord),
            event_stream=InMemoryRunEventLog(),
        )
        assert stores.resume_mode is ResumeMode.LIVE
        assert stores.is_durable is True

    def test_a_hot_layer_alone_is_not_durable(self):
        assert Stores(event_log=InMemoryRunEventLog()).is_durable is False

    def test_a_history_archive_makes_a_hot_log_durable(self):
        from agno_relay.stores import InMemoryHistoryArchive

        stores = Stores(
            event_log=InMemoryRunEventLog(),
            history_archive=InMemoryHistoryArchive(),
        )
        assert stores.resume_mode is ResumeMode.LIVE
        assert stores.is_durable is True


def _sortable(offset: str) -> tuple[int, ...]:
    """Redis offsets are ``ms-seq``; the others are plain integers."""
    return tuple(int(part) for part in offset.split("-"))
