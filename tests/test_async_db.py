from unittest.mock import AsyncMock, MagicMock

import pytest

from agno_harness.runtime.threads import ThreadService
from agno_harness.stores.redis_log import RedisRunEventLog


@pytest.mark.asyncio
async def test_thread_service_with_async_db():
    mock_db = MagicMock()
    mock_session = MagicMock()
    mock_session.session_id = "thread-123"
    mock_session.runs = [{"run_id": "run-1"}]
    mock_session.session_data = {"session_name": "Test Thread"}
    mock_session.updated_at = 1234567890
    mock_session.created_at = 1234567890

    mock_db.get_sessions = AsyncMock(return_value=[mock_session])
    mock_db.get_session = AsyncMock(return_value=mock_session)
    mock_db.delete_session = AsyncMock(return_value=True)

    service = ThreadService(db=mock_db)

    # 1. get_sessions should await get_sessions on db (projection kwargs)
    sessions = await service.get_sessions(user_id="user-1")
    assert len(sessions) == 1
    mock_db.get_sessions.assert_awaited_once()
    kwargs = mock_db.get_sessions.await_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs.get("deserialize") is False

    # 1b. list_threads reads from thread_store
    await service.stores.threads.start_turn("thread-123", user_id="user-1", title="Test Thread")
    threads = await service.list_threads(user_id="user-1")
    assert len(threads) == 1
    assert threads[0]["threadId"] == "thread-123"
    assert threads[0]["title"] == "Test Thread"
    assert threads[0]["messageCount"] == 0
    assert threads[0]["runCount"] == 1

    # 2. get_session should await get_session
    session = await service.get_session("thread-123", user_id="user-1")
    assert session == mock_session
    mock_db.get_session.assert_awaited_once_with(session_id="thread-123", user_id="user-1")

    # 3. delete_thread should await delete_session
    del_res = await service.delete_thread("thread-123", user_id="user-1")
    assert del_res["ok"] is True
    mock_db.delete_session.assert_awaited_once_with(session_id="thread-123", user_id="user-1")


@pytest.mark.redis
def test_redis_run_event_log_protocol():
    log = RedisRunEventLog.from_url("redis://127.0.0.1:6379/0", namespace="test", protocol=2)
    assert log.client.connection_pool.connection_kwargs.get("protocol") == 2


@pytest.mark.asyncio
async def test_thread_service_agent_scope():
    def session(sid: str, agent_id: str) -> MagicMock:
        s = MagicMock()
        s.session_id = sid
        s.agent_id = agent_id
        s.team_id = None
        s.workflow_id = None
        s.runs = []
        s.session_data = {}
        s.created_at = s.updated_at = 1
        return s

    mine, theirs = session("t-mine", "admin-agent"), session("t-theirs", "ipv-agent")
    db = MagicMock()
    db.get_sessions = AsyncMock(return_value=[mine, theirs])  # a db that ignores component_id
    db.get_session = AsyncMock(
        side_effect=lambda session_id, user_id: {"t-mine": mine, "t-theirs": theirs}[session_id]
    )
    db.delete_session = AsyncMock(return_value=True)

    service = ThreadService(db=db, agent_id="admin-agent")
    await service.stores.threads.start_turn("t-mine", user_id="u", agent_id="admin-agent")
    await service.stores.threads.start_turn("t-theirs", user_id="u", agent_id="ipv-agent")

    assert db.get_sessions.call_count == 0
    assert [s.session_id for s in await service.get_sessions(user_id="u")] == ["t-mine"]
    assert db.get_sessions.await_args.kwargs["component_id"] == "admin-agent"

    assert [t["threadId"] for t in await service.list_threads(user_id="u")] == ["t-mine"]
    assert await service.get_thread("t-theirs", user_id="u") is None
    assert await service.get_session("t-theirs", user_id="u") is None
    assert await service.replay_messages("t-theirs", user_id="u") is None

    # Deleting another agent's thread must not reach Agno's unscoped delete_session.
    result = await service.delete_thread("t-theirs", user_id="u")
    assert result["ok"] is False
    db.delete_session.assert_not_awaited()
    assert (await service.delete_thread("t-mine", user_id="u"))["ok"] is True
    db.delete_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_thread_service_per_call_agent_filter():
    db = MagicMock()
    db.get_sessions = AsyncMock(return_value=[])
    db.get_session = AsyncMock(return_value=None)

    # Unscoped service: a per-call agent_id filters down to one agent.
    service = ThreadService(db=db)
    await service.stores.threads.start_turn("t-a", user_id="u", agent_id="admin-agent")
    await service.stores.threads.start_turn("t-b", user_id="u", agent_id="ipv-agent")
    assert len(await service.list_threads(user_id="u")) == 2
    only_ipv = await service.list_threads(user_id="u", agent_id="ipv-agent")
    assert [t["threadId"] for t in only_ipv] == ["t-b"]
    assert only_ipv[0]["agentId"] == "ipv-agent"
    assert await service.get_thread("t-a", user_id="u", agent_id="ipv-agent") is None
    assert (await service.get_thread("t-a", user_id="u", agent_id="admin-agent"))[
        "threadId"
    ] == "t-a"
    assert (await service.delete_thread("t-a", user_id="u", agent_id="ipv-agent"))["ok"] is False
    assert (await service.delete_thread("t-a", user_id="u", agent_id="admin-agent"))["ok"] is True

    # Scoped service: a per-call value may repeat the scope but never switch it.
    scoped = ThreadService(db=db, agent_id="admin-agent")
    await scoped.stores.threads.start_turn("t-a", user_id="u", agent_id="admin-agent")
    await scoped.stores.threads.start_turn("t-b", user_id="u", agent_id="ipv-agent")
    assert len(await scoped.list_threads(user_id="u", agent_id="admin-agent")) == 1
    assert await scoped.list_threads(user_id="u", agent_id="ipv-agent") == []
    assert await scoped.get_thread("t-b", user_id="u", agent_id="ipv-agent") is None
    assert (await scoped.delete_thread("t-b", user_id="u", agent_id="ipv-agent"))["ok"] is False
