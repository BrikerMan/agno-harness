import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agno_harness.stores.sql_models import get_or_create_thread_model
from agno_harness.stores.thread_store import InMemoryThreadStore, SQLAlchemyThreadStore


@pytest.mark.asyncio
async def test_in_memory_thread_store_lifecycle():
    store = InMemoryThreadStore()

    # 1. Start turn
    thread = await store.start_turn(
        "t-1", user_id="user-a", run_id="r-1", title="Initial Title", metadata={"foo": "bar"}
    )
    assert thread["threadId"] == "t-1"
    assert thread["userId"] == "user-a"
    assert thread["title"] == "Initial Title"
    assert thread["status"] == "running"
    assert thread["isPaused"] is False
    assert thread["isError"] is False
    assert thread["runCount"] == 1
    assert thread["lastRunId"] == "r-1"
    assert thread["isDeleted"] is False
    assert thread["metadata"] == {"foo": "bar"}

    # 2. Pause
    await store.set_paused("t-1", is_paused=True, run_id="r-1")
    thread = await store.get_thread("t-1")
    assert thread is not None
    assert thread["status"] == "paused"
    assert thread["isPaused"] is True

    # 3. Resume / Unpause
    await store.set_paused("t-1", is_paused=False, run_id="r-1")
    thread = await store.get_thread("t-1")
    assert thread is not None
    assert thread["status"] == "running"
    assert thread["isPaused"] is False

    # 4. Finish
    await store.set_finished("t-1", is_error=False, run_id="r-1")
    thread = await store.get_thread("t-1")
    assert thread is not None
    assert thread["status"] == "finished"
    assert thread["lastFinishedAt"] is not None

    # 5. Start second turn and error
    await store.start_turn("t-1", user_id="user-a", run_id="r-2")
    thread = await store.get_thread("t-1")
    assert thread is not None
    assert thread["runCount"] == 2
    assert thread["lastRunId"] == "r-2"
    assert thread["status"] == "running"

    await store.set_finished("t-1", is_error=True, error_reason="TimeoutError", run_id="r-2")
    thread = await store.get_thread("t-1")
    assert thread is not None
    assert thread["status"] == "error"
    assert thread["isError"] is True
    assert thread["errorReason"] == "TimeoutError"

    # 6. Set Title
    await store.set_title("t-1", "Updated Title")
    thread = await store.get_thread("t-1")
    assert thread is not None
    assert thread["title"] == "Updated Title"

    # 7. List threads and user isolation
    await store.start_turn("t-2", user_id="user-b", run_id="r-3", title="User B Chat")
    user_a_threads = await store.list_threads(user_id="user-a")
    assert len(user_a_threads) == 1
    assert user_a_threads[0]["threadId"] == "t-1"

    all_threads = await store.list_threads()
    assert len(all_threads) == 2

    # 8. Soft Delete
    del_res = await store.delete_thread("t-1", user_id="user-a", hard=False)
    assert del_res is True

    # Should not appear in active list or get_thread unless include_deleted
    assert await store.get_thread("t-1") is None
    assert await store.get_thread("t-1", include_deleted=True) is not None
    active = await store.list_threads(user_id="user-a")
    assert len(active) == 0

    with_deleted = await store.list_threads(user_id="user-a", include_deleted=True)
    assert len(with_deleted) == 1
    assert with_deleted[0]["isDeleted"] is True

    # 9. Hard Delete
    hard_del = await store.delete_thread("t-1", user_id="user-a", hard=True)
    assert hard_del is True
    assert await store.get_thread("t-1", include_deleted=True) is None


@pytest.mark.asyncio
async def test_sqlalchemy_thread_store_lifecycle(tmp_path):
    db_file = tmp_path / "threads_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    model = get_or_create_thread_model("test_threads")
    async with engine.begin() as conn:
        await conn.run_sync(model.metadata.create_all)

    store = SQLAlchemyThreadStore(session_factory, model=model)

    # 1. Start turn
    t = await store.start_turn(
        "sql-1", user_id="alice", run_id="run-1", title="SQL Chat", metadata={"env": "test"}
    )
    assert t["threadId"] == "sql-1"
    assert t["userId"] == "alice"
    assert t["title"] == "SQL Chat"
    assert t["status"] == "running"
    assert t["runCount"] == 1
    assert t["lastRunId"] == "run-1"
    assert t["metadata"] == {"env": "test"}

    # 2. Pause and unpause
    await store.set_paused("sql-1", is_paused=True, run_id="run-1")
    t = await store.get_thread("sql-1")
    assert t is not None
    assert t["status"] == "paused"
    assert t["isPaused"] is True

    await store.set_paused("sql-1", is_paused=False, run_id="run-1")
    t = await store.get_thread("sql-1")
    assert t is not None
    assert t["status"] == "running"
    assert t["isPaused"] is False

    # 3. Finish and Error
    await store.set_finished("sql-1", is_error=True, error_reason="Crash")
    t = await store.get_thread("sql-1")
    assert t is not None
    assert t["status"] == "error"
    assert t["isError"] is True
    assert t["errorReason"] == "Crash"

    # 4. Update title
    await store.set_title("sql-1", "Renamed SQL Chat")
    t = await store.get_thread("sql-1")
    assert t is not None
    assert t["title"] == "Renamed SQL Chat"

    # 5. Multitenancy filtering
    await store.start_turn("sql-2", user_id="bob", run_id="run-2", title="Bob Chat")
    alice_threads = await store.list_threads(user_id="alice")
    assert len(alice_threads) == 1
    assert alice_threads[0]["threadId"] == "sql-1"

    # 6. Soft delete
    assert await store.delete_thread("sql-1", user_id="alice", hard=False) is True
    assert await store.get_thread("sql-1") is None
    t_del = await store.get_thread("sql-1", include_deleted=True)
    assert t_del is not None
    assert t_del["isDeleted"] is True

    # 7. Hard delete
    assert await store.delete_thread("sql-1", user_id="alice", hard=True) is True
    assert await store.get_thread("sql-1", include_deleted=True) is None

    await engine.dispose()
