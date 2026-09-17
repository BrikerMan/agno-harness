from unittest.mock import AsyncMock, MagicMock

import pytest

from agno_relay.runtime.threads import ThreadService
from agno_relay.stores.redis_log import RedisRunEventLog


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

    # 1. list_threads should await get_sessions (projection kwargs)
    threads = await service.list_threads(user_id="user-1")
    assert len(threads) == 1
    assert threads[0]["threadId"] == "thread-123"
    assert threads[0]["title"] == "Test Thread"
    assert threads[0]["messageCount"] == 0
    assert threads[0]["runCount"] == 1
    mock_db.get_sessions.assert_awaited_once()
    kwargs = mock_db.get_sessions.await_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs.get("deserialize") is False

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
