"""Runs that outlive their connection.

The load-bearing property is that detaching changes *nothing* about the event
stream. A client that reconnects must receive the same frames, in the same
order, as one that stayed — so most of these tests are equalities against a
direct ``stream_events`` call rather than assertions about individual frames.
"""

from __future__ import annotations

import asyncio

import pytest
from ag_ui.core import EventType

from agno_relay.core.log import RunStatus
from agno_relay.runtime.longrun import LongRunError, LongRunManager, RunNotOwned
from agno_relay.runtime.runtime import AguiRuntime
from agno_relay.runtime.translator import EVENT_RUN_CANCELLED, EVENT_RUN_PAUSED
from agno_relay.stores import InMemoryRunEventLog, Stores
from tests.conformance import assert_valid_agui_sequence
from tests.conftest import (
    FakeAgent,
    collect,
    content,
    make_input,
    run_completed,
    run_paused,
    tool_execution,
)


def chunks(*texts: str):
    return [content(text) for text in texts] + [run_completed("".join(texts))]


def runtime_for(agent: FakeAgent, **kwargs) -> AguiRuntime:
    return AguiRuntime(agent=agent, record_chunks=0, **kwargs)


def manager_for(agent: FakeAgent, *, log=None, **kwargs) -> LongRunManager:
    log = log if log is not None else InMemoryRunEventLog()
    return LongRunManager(runtime_for(agent, stores=Stores(event_log=log)), log=log, **kwargs)


async def drain(manager: LongRunManager, run_id: str, **kwargs) -> list[dict]:
    return [frame.event async for frame in manager.attach(run_id, **kwargs)]


async def settle(manager: LongRunManager, run_id: str) -> None:
    """Wait for the detached task, rather than sleeping and hoping."""
    task = manager._tasks.get(run_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)


class TestDetachedEqualsAttached:
    async def test_attaching_gives_the_same_stream_as_never_detaching(self):
        direct = await collect(
            runtime_for(FakeAgent(chunks("hello ", "world"))).stream_events(make_input())
        )

        manager = manager_for(FakeAgent(chunks("hello ", "world")))
        await manager.start(make_input())
        await settle(manager, "run-1")
        replayed = await drain(manager, "run-1")

        assert [event["type"] for event in replayed] == [e.type.value for e in direct]

    async def test_start_records_the_prompt_on_the_run(self):
        """So a reload can show the question before Agno has written the session."""
        log = InMemoryRunEventLog()
        manager = manager_for(FakeAgent(chunks("hi")), log=log)
        await manager.start(make_input("what is 2+2?"))
        record = await log.get_run("run-1")
        assert record is not None
        assert record.meta["input"] == "what is 2+2?"
        await settle(manager, "run-1")

    async def test_a_replayed_run_is_still_a_valid_agui_sequence(self):
        """The frames are what was sent, so they must survive strict mode."""
        manager = manager_for(FakeAgent(chunks("hi")))
        await manager.start(make_input())
        await settle(manager, "run-1")

        events = await drain(manager, "run-1")

        assert_valid_agui_sequence(_rehydrate(events))

    async def test_resuming_from_an_offset_joins_up_with_what_came_before(self):
        """The reconnect case: a half-drawn transcript plus the remainder.

        Getting this wrong duplicates or drops a token, which is invisible
        until it happens to land in the middle of somebody's sentence.
        """
        manager = manager_for(FakeAgent(chunks("one ", "two ", "three")))
        await manager.start(make_input())
        await settle(manager, "run-1")

        frames = [frame async for frame in manager.attach("run-1")]
        cut = frames[2]
        rest = await drain(manager, "run-1", after=cut.offset)

        assert [f.event for f in frames[:3]] + rest == [f.event for f in frames]

    async def test_a_failed_run_ends_as_an_error_frame_not_silence(self):
        manager = manager_for(FakeAgent(chunks("partial"), raise_at=1))
        await manager.start(make_input())
        await settle(manager, "run-1")

        events = await drain(manager, "run-1")

        assert events[-1]["type"] == EventType.RUN_ERROR.value
        assert (await manager.log.get_run("run-1")).status is RunStatus.ERROR


class TestIdempotence:
    async def test_starting_the_same_run_twice_runs_it_once(self):
        """Two tabs, one submit button, one charge."""
        agent = FakeAgent(chunks("hello"))
        manager = manager_for(agent)

        await manager.start(make_input())
        await manager.start(make_input())
        await settle(manager, "run-1")

        events = await drain(manager, "run-1")
        assert [e["type"] for e in events].count(EventType.RUN_STARTED.value) == 1

    async def test_a_finished_run_can_be_started_again(self):
        """Only *open* runs block a restart; re-running a finished id is a new stream."""
        manager = manager_for(FakeAgent(chunks("hello")))
        await manager.start(make_input())
        await settle(manager, "run-1")

        record = await manager.start(make_input())
        assert record.run_id == "run-1"
        assert record.status is RunStatus.RUNNING
        await settle(manager, "run-1")

        events = await drain(manager, "run-1")
        assert [e["type"] for e in events].count(EventType.RUN_STARTED.value) == 1


class TestLiveFollowing:
    async def test_attaching_mid_run_picks_up_the_rest(self):
        gate = asyncio.Event()
        manager = manager_for(_GatedAgent(gate, chunks("before ", "after")))

        await manager.start(make_input())
        received: list[dict] = []

        async def follow() -> None:
            async for frame in manager.attach("run-1"):
                received.append(frame.event)

        task = asyncio.create_task(follow())
        await asyncio.sleep(0.05)
        gate.set()
        await settle(manager, "run-1")
        await asyncio.wait_for(task, timeout=5)

        assert received[-1]["type"] == EventType.RUN_FINISHED.value
        assert "".join(e.get("delta", "") for e in received) == "before after"

    async def test_a_history_only_backend_returns_and_stops(self):
        """SQL and no Redis, run already over: attach is a read, not a wait.

        The important part is that it *ends*. A client left holding an open
        connection that will never emit another byte is worse off than one told
        plainly that there is no more to come.
        """
        manager = manager_for(FakeAgent(chunks("hi")), log=_HistoryOnlyLog())

        await manager.start(make_input())
        await settle(manager, "run-1")

        assert manager.can_follow_live is False
        assert await asyncio.wait_for(drain(manager, "run-1"), timeout=2)

    async def test_without_a_stream_the_process_running_it_can_still_follow(self):
        """Otherwise the no-Redis default shows nothing until the run finishes.

        The frames are already going past in this process on their way to the
        log, so handing them to a watcher costs nothing and needs no polling.
        What it cannot do is survive a restart or reach another worker, which is
        why it does not make ``can_follow_live`` true.
        """
        gate = asyncio.Event()
        manager = manager_for(_GatedAgent(gate, chunks("a", "b")), log=_HistoryOnlyLog())
        await manager.start(make_input())

        received: list[dict] = []

        async def follow() -> None:
            async for frame in manager.attach("run-1"):
                received.append(frame.event)
                gate.set()

        await asyncio.wait_for(follow(), timeout=5)

        assert received[-1]["type"] == EventType.RUN_FINISHED.value
        assert "".join(event.get("delta", "") for event in received) == "ab"

    async def test_following_starts_from_the_history_without_a_gap(self):
        """Late arrivals get the beginning too, exactly once."""
        gate = asyncio.Event()
        manager = manager_for(_GatedAgent(gate, chunks("a", "b")), log=_HistoryOnlyLog())
        await manager.start(make_input())
        await asyncio.sleep(0.05)  # let the first chunk reach the log

        async def release() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        asyncio.create_task(release())
        events = await asyncio.wait_for(drain(manager, "run-1"), timeout=5)

        assert "".join(event.get("delta", "") for event in events) == "ab"
        assert [e["type"] for e in events].count(EventType.RUN_STARTED.value) == 1

    async def test_another_worker_reads_history_and_stops(self):
        """A second manager on the same log is a second process, in miniature.

        It has no task for the run and no stream to subscribe to, so all it can
        honestly do is return what is stored — which is the degradation Redis
        exists to remove.
        """
        gate = asyncio.Event()
        log = _HistoryOnlyLog()
        manager = manager_for(_GatedAgent(gate, chunks("a", "b")), log=log)
        other = LongRunManager(runtime_for(FakeAgent([]), stores=Stores(event_log=log)), log=log)

        await manager.start(make_input())
        await asyncio.sleep(0.05)
        partial = await asyncio.wait_for(drain(other, "run-1"), timeout=2)
        gate.set()
        await settle(manager, "run-1")

        assert partial
        assert partial[-1]["type"] != EventType.RUN_FINISHED.value


class TestAbort:
    async def test_aborting_stops_a_run_in_flight(self):
        gate = asyncio.Event()
        manager = manager_for(_GatedAgent(gate, chunks("a", "b")))
        await manager.start(make_input())
        await asyncio.sleep(0.05)

        assert await manager.abort("run-1") is True
        assert (await manager.log.get_run("run-1")).status is RunStatus.ABORTED

        # The cancel frame was appended to the log
        frames = await manager.log.read("run-1")
        assert frames
        last_event = frames[-1].event
        assert last_event.get("type") == "CUSTOM"
        assert last_event.get("name") == EVENT_RUN_CANCELLED
        assert last_event.get("value", {}).get("reason") == "user_aborted"

    async def test_follower_receives_run_cancelled_event(self):
        gate = asyncio.Event()
        manager = manager_for(_GatedAgent(gate, chunks("a", "b")))
        await manager.start(make_input())
        await asyncio.sleep(0.05)

        # Attach follower
        received: list[dict] = []

        async def follow():
            async for frame in manager.attach("run-1"):
                received.append(frame.event)

        task = asyncio.create_task(follow())
        await asyncio.sleep(0.05)
        await manager.abort("run-1")
        await asyncio.wait_for(task, timeout=2.0)

        assert any(e.get("name") == EVENT_RUN_CANCELLED for e in received)

    async def test_aborting_in_flight_archives_run_cancelled_event(self):
        from agno_relay.stores import InMemoryHistoryArchive

        archive = InMemoryHistoryArchive()
        log = InMemoryRunEventLog()
        runtime = AguiRuntime(
            agent=_GatedAgent(asyncio.Event(), chunks("a", "b")),
            stores=Stores(event_log=log, history_archive=archive),
        )
        manager = LongRunManager(runtime, log=log)
        await manager.start(make_input())
        await asyncio.sleep(0.05)

        assert await manager.abort("run-1") is True

        archived_runs = await archive.list_thread("thread-1")
        assert len(archived_runs) == 1
        run_id, events = archived_runs[0]
        assert run_id == "run-1"
        assert any(e.get("name") == EVENT_RUN_CANCELLED for e in events)

    async def test_aborting_a_finished_run_reports_nothing_to_do(self):
        manager = manager_for(FakeAgent(chunks("hi")))
        await manager.start(make_input())
        await settle(manager, "run-1")

        assert await manager.abort("run-1") is False

    async def test_aborting_an_unknown_run_is_not_an_error(self):
        assert await manager_for(FakeAgent()).abort("nope") is False


class TestShutdown:
    async def test_a_restart_ends_in_flight_runs_instead_of_abandoning_them(self):
        """Otherwise the log keeps saying ``running`` for a task that is gone."""
        gate = asyncio.Event()
        manager = manager_for(_GatedAgent(gate, chunks("a")))
        await manager.start(make_input())
        await asyncio.sleep(0.05)

        await manager.shutdown()

        record = await manager.log.get_run("run-1")
        assert record.status is RunStatus.ERROR
        assert "restarted" in record.error


class TestActiveRuns:
    async def test_finished_runs_drop_off_the_active_list(self):
        manager = manager_for(FakeAgent(chunks("hi")))
        await manager.start(make_input())
        await settle(manager, "run-1")

        assert await manager.list_active("thread-1") == []

    async def test_a_paused_run_stays_active(self):
        """HITL: the run waiting on an answer is the one the user came back for.

        Dropping it from the list loses it entirely — Agno's session stays
        locked in the paused state and no client knows to ask.
        """
        manager = manager_for(FakeAgent(chunks("hi")))
        await manager.start(make_input())
        await settle(manager, "run-1")
        await manager.log.set_status("run-1", RunStatus.PAUSED)

        assert [r.run_id for r in await manager.list_active("thread-1")] == ["run-1"]

    async def test_attaching_to_a_paused_run_ends_after_stored_frames(self):
        """HITL: the stream must close so the client can unlock the form."""
        manager = manager_for(
            FakeAgent(
                [
                    content("May I?"),
                    run_paused(
                        [
                            tool_execution(
                                "c1",
                                "send_message",
                                {"recipient": "x"},
                                requires_user_input=True,
                            )
                        ]
                    ),
                ]
            )
        )
        await manager.start(make_input())
        await settle(manager, "run-1")
        assert (await manager.log.get_run("run-1")).status is RunStatus.PAUSED

        frames = await asyncio.wait_for(drain(manager, "run-1"), timeout=2)
        assert any(
            frame.get("type") == "CUSTOM" and frame.get("name") == EVENT_RUN_PAUSED
            for frame in frames
        )


class TestOwnership:
    async def test_another_user_cannot_attach(self):
        manager = manager_for(FakeAgent(chunks("secret")))
        await manager.start(make_input(), user_id="alice")
        await settle(manager, "run-1")

        with pytest.raises(RunNotOwned):
            await drain(manager, "run-1", user_id="bob")

    async def test_another_user_cannot_abort(self):
        gate = asyncio.Event()
        manager = manager_for(_GatedAgent(gate, chunks("a")))
        await manager.start(make_input(), user_id="alice")
        await asyncio.sleep(0.05)

        with pytest.raises(RunNotOwned):
            await manager.abort("run-1", user_id="bob")
        gate.set()

    async def test_another_user_cannot_hijack_a_run_by_starting_it(self):
        """Idempotence must not become a way in."""
        gate = asyncio.Event()
        manager = manager_for(_GatedAgent(gate, chunks("a")))
        await manager.start(make_input(), user_id="alice")

        with pytest.raises(RunNotOwned):
            await manager.start(make_input(), user_id="bob")
        gate.set()

    async def test_the_owner_still_gets_through(self):
        manager = manager_for(FakeAgent(chunks("mine")))
        await manager.start(make_input(), user_id="alice")
        await settle(manager, "run-1")

        assert await drain(manager, "run-1", user_id="alice")


class TestFailOpen:
    async def test_a_broken_log_does_not_kill_the_run(self):
        """Somebody is watching this run; a recording fault is not their problem.

        The run finishes and is flagged instead, which is what the resume
        header then reports.
        """
        log = _BrokenLog()
        manager = manager_for(FakeAgent(chunks("hello")), log=log)

        await manager.start(make_input())
        await settle(manager, "run-1")

        record = await log.get_run("run-1")
        assert record.status is RunStatus.FINISHED
        assert record.unrecordable is True


class TestAssembly:
    def test_a_manager_without_a_log_is_refused_at_assembly(self):
        """Failing here beats a frontend stuck on "reconnecting" forever."""
        with pytest.raises(LongRunError, match="RunEventLog"):
            LongRunManager(runtime_for(FakeAgent()))

    def test_the_log_is_taken_from_the_stores_when_not_passed(self):
        log = InMemoryRunEventLog()
        manager = LongRunManager(runtime_for(FakeAgent(), stores=Stores(event_log=log)))
        assert manager.log is log
        assert manager.can_follow_live is True


class _GatedAgent(FakeAgent):
    """Blocks after its first chunk until released, so mid-run is reachable."""

    def __init__(self, gate: asyncio.Event, chunks) -> None:
        super().__init__(chunks)
        self.gate = gate

    async def _stream(self):
        first, *rest = self.chunks
        yield first
        await self.gate.wait()
        for chunk in rest:
            yield chunk


class _HistoryOnlyLog:
    """Durable but not tailable, the way a SQL log is.

    A wrapper rather than a subclass because ``RunEventStream`` is detected by
    the presence of ``tail``, and a subclass cannot un-inherit a method.
    """

    is_durable = True

    def __init__(self) -> None:
        self._inner = InMemoryRunEventLog()

    def __getattr__(self, name: str):
        if name == "tail":
            raise AttributeError(name)
        return getattr(self._inner, name)


class _BrokenLog(InMemoryRunEventLog):
    async def append(self, run_id, frames):
        raise RuntimeError("the disk went away")


def _rehydrate(events: list[dict]):
    from ag_ui.core import Event
    from pydantic import TypeAdapter

    adapter = TypeAdapter(Event)
    return [adapter.validate_python(event) for event in events]
