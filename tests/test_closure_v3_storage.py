"""Seal and checkpoint must survive a round trip through real Agno v3 storage.

Agno v3 keeps each run in its own table and ``upsert_session`` no longer
rewrites runs. A fake db hides that, so these tests read back through a fresh
connection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agno.db.sqlite import AsyncSqliteDb, SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from agno_harness.runtime.closure import attach_checkpoint_to_session, seal_session_run

SESSION_ID = "s-v3"
RUN_ID = "r-v3"


def _cancelled_run() -> RunOutput:
    return RunOutput(
        run_id=RUN_ID,
        agent_id="agent",
        session_id=SESSION_ID,
        status=RunStatus.cancelled,
        content="partial",
        messages=[
            Message(role="user", content="go"),
            Message(
                role="assistant",
                content="working",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "slow", "arguments": "{}"},
                    }
                ],
            ),
        ],
    )


def _seed_sync(db_file: Path) -> SqliteDb:
    db = SqliteDb(db_file=str(db_file))
    run = _cancelled_run()
    db.upsert_session(AgentSession(session_id=SESSION_ID, agent_id="agent", runs=[run]))
    db.upsert_run(run, session_id=SESSION_ID)
    return db


def test_seal_persists_to_runs_table(tmp_path: Path) -> None:
    db_file = tmp_path / "agno.db"
    db = _seed_sync(db_file)

    assert asyncio.run(seal_session_run(db, SESSION_ID, RUN_ID, reason="stopped")) is True

    reader = SqliteDb(db_file=str(db_file))
    run = reader.get_runs(session_id=SESSION_ID)[0]
    assert run.status == RunStatus.cancelled
    assert run.metadata["sealed"] is True
    assert run.metadata["interruption_reason"] == "stopped"
    assert run.messages[-1].role == "tool"
    assert run.messages[-1].tool_call_id == "call_1"

    session = reader.get_session(session_id=SESSION_ID)
    assert session is not None
    assert session.runs[0].metadata["sealed"] is True


def test_checkpoint_persists_run_and_session(tmp_path: Path) -> None:
    db_file = tmp_path / "agno.db"
    db = _seed_sync(db_file)
    checkpoint = {"content": "# CONTEXT CHECKPOINT\nfacts"}

    assert asyncio.run(attach_checkpoint_to_session(db, SESSION_ID, RUN_ID, checkpoint)) is True

    reader = SqliteDb(db_file=str(db_file))
    run = reader.get_runs(session_id=SESSION_ID)[0]
    assert run.metadata["checkpoint"] == checkpoint
    session = reader.get_session(session_id=SESSION_ID)
    assert session is not None
    assert session.session_data["latest_checkpoint"] == checkpoint
    assert session.session_data["latest_checkpoint_run_id"] == RUN_ID


def test_seal_and_checkpoint_persist_on_async_db(tmp_path: Path) -> None:
    db_file = tmp_path / "agno_async.db"

    async def scenario() -> None:
        db = AsyncSqliteDb(db_file=str(db_file))
        run = _cancelled_run()
        await db.upsert_session(AgentSession(session_id=SESSION_ID, agent_id="agent", runs=[run]))
        await db.upsert_run(run, session_id=SESSION_ID)

        assert await seal_session_run(db, SESSION_ID, RUN_ID, reason="stopped") is True
        checkpoint = {"content": "summary"}
        assert await attach_checkpoint_to_session(db, SESSION_ID, RUN_ID, checkpoint) is True

        reader = AsyncSqliteDb(db_file=str(db_file))
        runs = await reader.get_runs(session_id=SESSION_ID)
        assert runs[0].metadata["sealed"] is True
        assert runs[0].metadata["checkpoint"] == checkpoint
        assert runs[0].messages[-1].role == "tool"

    asyncio.run(scenario())


def test_seal_waits_for_agno_to_flush_the_interrupted_run(tmp_path: Path) -> None:
    """Agno writes the cancelled run from its own cleanup; sealing must not race it."""
    db_file = tmp_path / "agno_late.db"
    db = SqliteDb(db_file=str(db_file))
    # An earlier, completed turn is already stored; the interrupted one is not.
    earlier = RunOutput(
        run_id="r-earlier",
        agent_id="agent",
        session_id=SESSION_ID,
        status=RunStatus.completed,
        content="done",
        messages=[Message(role="user", content="hi"), Message(role="assistant", content="done")],
    )
    db.upsert_session(AgentSession(session_id=SESSION_ID, agent_id="agent", runs=[earlier]))
    db.upsert_run(earlier, session_id=SESSION_ID)

    async def scenario() -> bool:
        async def flush_later() -> None:
            await asyncio.sleep(0.1)
            db.upsert_run(_cancelled_run(), session_id=SESSION_ID)

        flusher = asyncio.create_task(flush_later())
        ok = await seal_session_run(db, SESSION_ID, RUN_ID, reason="stopped")
        await flusher
        return ok

    assert asyncio.run(scenario()) is True
    runs = {r.run_id: r for r in SqliteDb(db_file=str(db_file)).get_runs(session_id=SESSION_ID)}
    assert runs[RUN_ID].metadata["sealed"] is True
    assert not (runs["r-earlier"].metadata or {}).get("sealed")


def test_sealed_run_reaches_next_turn_history_from_real_db(tmp_path: Path) -> None:
    """A sealed cancelled run is replayed to the model; an unsealed one is not."""
    from agno_harness.runtime.closure import install_sealed_history_hook

    install_sealed_history_hook()
    db_file = tmp_path / "agno_hist.db"
    db = _seed_sync(db_file)

    before = SqliteDb(db_file=str(db_file)).get_session(session_id=SESSION_ID)
    assert before is not None
    assert before.get_messages() == []

    asyncio.run(seal_session_run(db, SESSION_ID, RUN_ID, reason="stopped"))

    after = SqliteDb(db_file=str(db_file)).get_session(session_id=SESSION_ID)
    assert after is not None
    history = after.get_messages()
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert history[-1].tool_call_id == "call_1"
    # The persisted status is untouched by the hook.
    assert after.runs[0].status == RunStatus.cancelled
