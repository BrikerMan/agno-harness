"""Replay, and the live/replay parity guarantee.

The parity tests are the important ones. They run a scenario live, rebuild the
same thread from storage, and assert a client would render the two identically —
which is what stops a reload from changing what the user sees.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agno_harness import AgentRuntime, SequencerMode
from agno_harness.runtime.longrun import LongRunManager
from agno_harness.runtime.replay import (
    input_to_text,
    last_user_text,
    run_to_messages,
    session_title,
    session_to_messages,
)
from agno_harness.runtime.threads import FramesUnavailable
from agno_harness.stores import InMemoryCustomEventStore, InMemoryRunEventLog, Stores

from .conformance import assert_valid_agui_sequence, messages_from_events
from .conftest import FakeAgent, content, make_input, run_completed

# ── fake persisted objects, shaped like Agno's ────────────────────────────


@dataclass
class FakeToolExecution:
    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    tool_call_error: bool = False
    metrics: Any = None


@dataclass
class FakeInput:
    input_content: Any


@dataclass
class FakeRun:
    run_id: str
    input: FakeInput | None = None
    content: str = ""
    reasoning_content: str | None = None
    tools: list[FakeToolExecution] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeSession:
    session_id: str
    runs: list[FakeRun] = field(default_factory=list)
    updated_at: int = 0
    user_id: str | None = None
    session_data: dict[str, Any] | None = None


class FakeDb:
    """Stands in for Agno's ``BaseDb``, filtering by user the way it does.

    Filtering here rather than in the assertions is the point: it is what
    catches the toolbox reading a session and *then* checking who owns it,
    which is the mistake that leaks a thread's existence.
    """

    def __init__(self, sessions: list[FakeSession]):
        self.sessions = {s.session_id: s for s in sessions}
        self.deleted: list[str] = []
        self.queried_user_ids: list[str | None] = []

    def get_session(self, session_id: str, user_id: str | None = None, **_: Any):
        self.queried_user_ids.append(user_id)
        session = self.sessions.get(session_id)
        if session is None or not _owned_by(session, user_id):
            return None
        return session

    def get_sessions(self, user_id: str | None = None, **_: Any):
        self.queried_user_ids.append(user_id)
        return [s for s in self.sessions.values() if _owned_by(s, user_id)]

    def delete_session(self, session_id: str, user_id: str | None = None):
        self.queried_user_ids.append(user_id)
        session = self.sessions.get(session_id)
        if session is None or not _owned_by(session, user_id):
            return False
        self.deleted.append(session_id)
        self.sessions.pop(session_id, None)
        return True

    def rename_session(
        self,
        session_id: str,
        session_type: Any = None,
        session_name: str = "",
        user_id: str | None = None,
        **_: Any,
    ):
        session = self.get_session(session_id, user_id=user_id)
        if session is None:
            return None
        if session.session_data is None:
            session.session_data = {}
        session.session_data["session_name"] = session_name
        return session


def _owned_by(session: FakeSession, user_id: str | None) -> bool:
    return user_id is None or session.user_id == user_id


class TestRunToMessages:
    def test_a_run_becomes_a_user_and_an_assistant_message(self):
        run = FakeRun(run_id="r1", input=FakeInput("What is 2+2?"), content="Four.")
        messages = run_to_messages(run)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "What is 2+2?"
        assert messages[1]["content"] == "Four."

    def test_the_streamui_fence_is_extracted_from_persisted_text(self):
        run = FakeRun(
            run_id="r1",
            input=FakeInput("stats"),
            content='Here:\n```stream-ui {"schema": "demo-card"}\n{"type":"stat","v":1}\n```\nDone.',
        )
        assistant = run_to_messages(run)[1]
        assert assistant["content"] == "Here:\nDone."
        assert assistant["uiBlocks"][0]["items"][0]["data"] == {"type": "stat", "v": 1}

    def test_hidden_tools_stay_hidden_in_history(self):
        run = FakeRun(
            run_id="r1",
            input=FakeInput("hi"),
            content="Done.",
            tools=[
                FakeToolExecution("c1", "load_skill", result="internal"),
                FakeToolExecution("c2", "search", result="public"),
            ],
        )
        assistant = run_to_messages(run, hidden_tool_names={"load_skill"})[1]
        assert [t["name"] for t in assistant["toolCalls"]] == ["search"]

    def test_custom_events_ride_along_on_the_assistant_message(self):
        run = FakeRun(run_id="r1", input=FakeInput("hi"), content="Hello.")
        assistant = run_to_messages(run, custom_events=[{"name": "billing", "value": {}}])[1]
        assert assistant["customEvents"][0]["name"] == "billing"

    def test_checkpoint_metadata_synthesizes_context_compression_event(self):
        run = FakeRun(
            run_id="r1",
            input=FakeInput("hi"),
            content="Hello.",
            metadata={
                "checkpoint": {
                    "original_tokens": 8000,
                    "compacted_tokens": 4000,
                    "saved_tokens": 4000,
                    "content": "Handover text",
                }
            },
        )
        assistant = run_to_messages(run)[1]
        assert len(assistant["customEvents"]) == 1
        event = assistant["customEvents"][0]
        assert event["name"] == "context.compression"
        assert event["value"]["original_tokens"] == 8000
        assert event["value"]["compacted_tokens"] == 4000
        assert event["value"]["checkpoint"] == "Handover text"

    def test_a_run_with_only_an_injected_event_still_produces_a_message(self):
        """A run whose only output was a hook's event still has something to show."""
        run = FakeRun(run_id="r1", input=FakeInput("hi"))
        messages = run_to_messages(run, custom_events=[{"name": "billing", "value": {"cost": 1}}])
        assert [m["role"] for m in messages] == ["user", "assistant"]

    def test_a_run_with_nothing_to_show_yields_no_assistant_message(self):
        messages = run_to_messages(FakeRun(run_id="r1", input=FakeInput("hi")))
        assert [m["role"] for m in messages] == ["user"]

    def test_reasoning_is_carried_over(self):
        run = FakeRun(
            run_id="r1", input=FakeInput("hi"), content="Answer.", reasoning_content="Thinking."
        )
        assert run_to_messages(run)[1]["reasoning"] == "Thinking."


class TestInputExtraction:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ""),
            (FakeInput("plain"), "plain"),
            (FakeInput(None), ""),
        ],
    )
    def test_shapes(self, value, expected):
        assert input_to_text(value) == expected

    def test_the_latest_user_turn_is_the_prompt(self):
        from types import SimpleNamespace

        assert last_user_text(None) == ""
        assert (
            last_user_text(
                [
                    SimpleNamespace(role="user", content="first"),
                    SimpleNamespace(role="assistant", content="ok"),
                    SimpleNamespace(role="user", content="second"),
                ]
            )
            == "second"
        )


class TestSessions:
    def test_runs_flatten_oldest_first(self):
        session = FakeSession(
            "t1",
            runs=[
                FakeRun("r1", FakeInput("first"), "one"),
                FakeRun("r2", FakeInput("second"), "two"),
            ],
        )
        messages = session_to_messages(session)
        assert [m["content"] for m in messages] == ["first", "one", "second", "two"]

    def test_the_title_is_the_first_user_message(self):
        session = FakeSession("t1", runs=[FakeRun("r1", FakeInput("How do I ship?"), "…")])
        assert session_title(session) == "How do I ship?"

    def test_a_saved_name_wins_over_the_first_user_message(self):
        session = FakeSession(
            "t1",
            runs=[FakeRun("r1", FakeInput("How do I ship?"), "…")],
            session_data={"session_name": "Shipping checklist"},
        )
        assert session_title(session) == "Shipping checklist"

    def test_an_empty_session_has_a_placeholder_title(self):
        assert session_title(FakeSession("t1")) == "(empty)"


class TestRuntimeHistory:
    async def test_replay_returns_none_for_an_unknown_thread(self):
        runtime = AgentRuntime(agent=FakeAgent(), db=FakeDb([]))
        assert await runtime.replay_messages("nope") is None

    async def test_an_inflight_prompt_shows_before_agno_flushes(self):
        """Agno writes the session when the run ends. The hot log has the
        prompt from ``start``, so a reload mid-run can still name the question.
        """
        log = InMemoryRunEventLog()
        await log.start_run("r-live", "t1", user_id="alice", input="still going?")
        runtime = AgentRuntime(
            agent=FakeAgent(),
            db=FakeDb(
                [
                    FakeSession(
                        "t1",
                        runs=[FakeRun("r-old", FakeInput("earlier"), "done.")],
                        user_id="alice",
                    )
                ]
            ),
            stores=Stores(event_log=log),
        )
        messages = await runtime.replay_messages("t1", user_id="alice")
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert messages[-1]["content"] == "still going?"

    async def test_a_brand_new_thread_still_has_its_inflight_prompt(self):
        log = InMemoryRunEventLog()
        await log.start_run("r1", "t-new", input="hello")
        runtime = AgentRuntime(agent=FakeAgent(), db=FakeDb([]), stores=Stores(event_log=log))
        messages = await runtime.replay_messages("t-new")
        assert messages == [{"id": "u-r1", "role": "user", "content": "hello"}]

    async def test_replay_attaches_persisted_custom_events_to_their_run(self):
        store = InMemoryCustomEventStore()
        await store.save("t1", "r1", "billing", {"cost": 7})
        runtime = AgentRuntime(
            agent=FakeAgent(),
            db=FakeDb([FakeSession("t1", runs=[FakeRun("r1", FakeInput("hi"), "Hello.")])]),
            stores=Stores(custom_events=store),
        )
        messages = await runtime.replay_messages("t1")
        assert messages[1]["customEvents"] == [{"name": "billing", "value": {"cost": 7}}]

    async def test_threads_list_newest_first(self):
        runtime = AgentRuntime(
            agent=FakeAgent(),
            db=FakeDb(
                [
                    FakeSession("old", runs=[FakeRun("r1", FakeInput("older"), "a")], updated_at=1),
                    FakeSession("new", runs=[FakeRun("r2", FakeInput("newer"), "b")], updated_at=2),
                ]
            ),
        )
        threads = await runtime.list_threads()
        assert [t["threadId"] for t in threads] == ["new", "old"]
        assert threads[0]["title"] == "newer"
        assert threads[0]["messageCount"] == 0
        assert threads[0]["runCount"] == 1

    async def test_deleting_a_thread_also_clears_toolbox_records(self):
        store = InMemoryCustomEventStore()
        await store.save("t1", "r1", "billing", {})
        db = FakeDb([FakeSession("t1", runs=[FakeRun("r1", FakeInput("hi"), "a")])])
        runtime = AgentRuntime(agent=FakeAgent(), db=db, stores=Stores(custom_events=store))
        assert (await runtime.delete_thread("t1"))["ok"] is True
        assert db.deleted == ["t1"]
        assert await store.list_by_thread("t1") == []

    async def test_a_failed_delete_reports_the_error(self):
        class BrokenDb(FakeDb):
            def delete_session(self, session_id, user_id=None):
                raise RuntimeError("locked")

        runtime = AgentRuntime(agent=FakeAgent(), db=BrokenDb([]))
        result = await runtime.delete_thread("t1")
        assert result["ok"] is False
        assert "locked" in result["error"]


class TestFrameReplay:
    """Replay by re-sending. There is no second renderer left to disagree with.

    The old test in this position streamed a scenario, replayed it out of a
    stored session, and compared the two — because they were two different
    implementations of the same rendering. Now the stored thing *is* what was
    sent, so what needs checking is the round trip: the frames come back in
    order, the cursor is right, and a client reducing them lands where it landed
    live.
    """

    async def _run(self, chunks, *, thread_id="thread-1", run_id="run-1"):
        log = await sql_event_log()
        runtime = AgentRuntime(
            agent=FakeAgent(chunks),
            stores=Stores(event_log=log),
            sequencer_mode=SequencerMode.AUDIT,
            allow_ephemeral_agno_db=True,
        )
        manager = LongRunManager(runtime, log=log)
        await manager.start(make_input(thread_id=thread_id, run_id=run_id))
        task = manager._tasks.get(run_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        return runtime, log

    async def test_a_thread_replays_as_the_frames_that_were_sent(self):
        runtime, _ = await self._run([content("Hello, "), content("world."), run_completed()])

        frames = await runtime.threads.read_frames("thread-1")

        assert [f["event"]["type"] for f in frames] == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]

    async def test_reducing_the_frames_gives_what_the_live_stream_gave(self):
        """The property the old parity test was reaching for, now trivially true."""
        answer = 'Look:\n```stream-ui {"schema": "demo-card"}\n{"type":"stat","v":1}\n```\nDone.'
        chunks = [content(answer[i : i + 5]) for i in range(0, len(answer), 5)]
        runtime, _ = await self._run([*chunks, run_completed()])

        frames = await runtime.threads.read_frames("thread-1")
        rebuilt = messages_from_events(_rehydrate(f["event"] for f in frames))

        assert rebuilt[0]["content"] == "Look:\nDone."
        assert len(rebuilt[0]["uiBlocks"][0]["items"]) == 1

    async def test_the_replayed_frames_are_a_valid_agui_sequence(self):
        runtime, _ = await self._run([content("hi"), run_completed()])

        frames = await runtime.threads.read_frames("thread-1")

        assert_valid_agui_sequence(_rehydrate(f["event"] for f in frames))

    async def test_a_cursor_resumes_where_it_left_off(self):
        runtime, _ = await self._run([content("a"), content("b"), run_completed()])

        frames = await runtime.threads.read_frames("thread-1")
        rest = await runtime.threads.read_frames("thread-1", after=frames[1]["id"])

        assert rest == frames[2:]

    async def test_every_run_in_a_thread_comes_back_in_order(self):
        log = await sql_event_log()
        runtime = AgentRuntime(
            agent=FakeAgent([]),
            stores=Stores(event_log=log),
            allow_ephemeral_agno_db=True,
        )
        manager = LongRunManager(runtime, log=log)
        for index, word in enumerate(("first", "second")):
            runtime.agent.chunks = [content(word), run_completed()]
            await manager.start(make_input(thread_id="t1", run_id=f"r{index}"))
            await asyncio.wait_for(asyncio.shield(manager._tasks[f"r{index}"]), timeout=5)

        frames = await runtime.threads.read_frames("t1")

        deltas = [
            f["event"]["delta"] for f in frames if f["event"]["type"] == "TEXT_MESSAGE_CONTENT"
        ]
        assert deltas == ["first", "second"]

    async def test_a_delegation_replays_from_its_own_frames(self):
        """No ``subagent.run`` record any more; the bracket and its contents are
        in the log like everything else."""
        from agno_harness import substream

        log = await sql_event_log()
        runtime = AgentRuntime(
            agent=FakeAgent([]),
            stores=Stores(event_log=log),
            allow_ephemeral_agno_db=True,
        )

        async def parent_stream(**kwargs):
            async with substream("reviewer", description="Reviewing add()") as emit:
                await emit(content("The sign is wrong."))
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        manager = LongRunManager(runtime, log=log)
        await manager.start(make_input(thread_id="t-sub", run_id="r-sub"))
        await asyncio.wait_for(asyncio.shield(manager._tasks["r-sub"]), timeout=5)

        frames = [f["event"] for f in await runtime.threads.read_frames("t-sub")]
        names = [f.get("name") for f in frames if f["type"] == "CUSTOM"]

        assert names == ["subagent.start", "subagent.end"]
        start = next(i for i, f in enumerate(frames) if f.get("name") == "subagent.start")
        end = next(i for i, f in enumerate(frames) if f.get("name") == "subagent.end")
        inner = "".join(
            f.get("delta", "") for f in frames[start:end] if f["type"] == "TEXT_MESSAGE_CONTENT"
        )
        assert inner == "The sign is wrong."

    async def test_the_archive_folds_content_deltas_and_still_reduces(self):
        """SQL keeps one CONTENT event; Redis kept every token for resume."""
        from agno_harness.stores import InMemoryHistoryArchive

        log = InMemoryRunEventLog()
        runtime = AgentRuntime(
            agent=FakeAgent([content("Hel"), content("lo."), run_completed()]),
            stores=Stores(event_log=log, history_archive=InMemoryHistoryArchive()),
            sequencer_mode=SequencerMode.AUDIT,
        )
        manager = LongRunManager(runtime, log=log)
        await manager.start(make_input(thread_id="t-arc", run_id="r-arc"))
        await asyncio.wait_for(asyncio.shield(manager._tasks["r-arc"]), timeout=5)

        frames = await runtime.threads.read_frames("t-arc")
        contents = [f["event"] for f in frames if f["event"]["type"] == "TEXT_MESSAGE_CONTENT"]
        assert len(contents) == 1
        assert contents[0]["delta"] == "Hello."
        rebuilt = messages_from_events(_rehydrate(f["event"] for f in frames))
        assert rebuilt[0]["content"] == "Hello."


class TestFramesRequireALog:
    async def test_no_log_at_all_is_refused(self):
        """Rather than an empty list, which reads as "the thread is empty"."""
        runtime = AgentRuntime(agent=FakeAgent())
        with pytest.raises(FramesUnavailable, match="history archive or a RunEventLog"):
            await runtime.threads.read_frames("t1")

    async def test_a_hot_layer_alone_is_replayable_while_it_remembers(self):
        """Redis replay: the TTL is the horizon, not a reason to 501."""
        log = InMemoryRunEventLog()
        await log.start_run("r1", "t1")
        await log.append("r1", [{"type": "RUN_STARTED"}])
        runtime = AgentRuntime(agent=FakeAgent(), stores=Stores(event_log=log))
        frames = await runtime.threads.read_frames("t1")
        assert [f["event"]["type"] for f in frames] == ["RUN_STARTED"]


async def sql_event_log():
    """A durable log, which is what frame replay requires.

    SQLite standing in for whatever the deployment uses. The in-memory log will
    not do here on purpose: it does not survive a restart, and replay served
    from something that forgets is the failure ``is_durable`` exists to prevent.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase

    from agno_harness.stores import RunFrameMixin, RunRecordMixin, SQLRunEventLog

    class Base(DeclarativeBase):
        pass

    class Frame(Base, RunFrameMixin):
        __tablename__ = "frames"

    class Record(Base, RunRecordMixin):
        __tablename__ = "records"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return SQLRunEventLog(async_sessionmaker(engine, expire_on_commit=False), Frame, Record)


def _rehydrate(events):
    from ag_ui.core import Event
    from pydantic import TypeAdapter

    adapter = TypeAdapter(Event)
    return [adapter.validate_python(event) for event in events]
