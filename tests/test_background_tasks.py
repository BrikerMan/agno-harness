import asyncio
import os
from types import SimpleNamespace

import pytest

from agno_harness.background_task.context import bind_origin, reset_origin
from agno_harness.background_task.handlers import SleepHandler
from agno_harness.background_task.service import INTERRUPTED, BackgroundTaskService
from agno_harness.background_task.toolkit import BackgroundTaskToolkit
from agno_harness.core.channel import OutboundMessage
from agno_harness.db import AgnoHarnessSqliteDb
from agno_harness.sessions.models import ConversationKey


class _Capture:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, destination: ConversationKey, message) -> str:
        self.messages.append(message.text)
        return "1"


@pytest.mark.asyncio
async def test_background_task_stores_runs_and_replies(tmp_path) -> None:
    service = BackgroundTaskService(str(tmp_path / "agent.db"))
    service.register(SleepHandler())
    channel = _Capture()
    service.bind(SimpleNamespace(channels={"teams": (channel, None)}))
    key = ConversationKey(platform="teams", chat_id="19:chat", is_direct_message=True)
    token = bind_origin("teams", key)
    try:
        record, message = await service.create(
            task_type="sleep",
            subject="probe",
            params={"seconds": 0},
        )
    finally:
        reset_origin(token)

    assert record is not None
    assert "Created background task #" in message
    found = record
    for _ in range(50):
        loaded = await service.get(record.id)
        assert loaded is not None
        found = loaded
        if found.status == "done":
            break
        await asyncio.sleep(0.02)

    assert found.status == "done"
    assert found.result == "Finished waiting 0 seconds."
    assert channel.messages
    assert "finished" in channel.messages[0]
    assert "Finished waiting 0 seconds." in channel.messages[0]


class _Hold:
    task_type = "hold"
    description = "Wait until released."

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def run(self, task) -> str:
        await self.gate.wait()
        return "released"


@pytest.mark.asyncio
async def test_created_task_is_listed_while_running_then_done(tmp_path) -> None:
    service = BackgroundTaskService(str(tmp_path / "agent.db"))
    hold = _Hold()
    service.register(hold)
    channel = _Capture()
    service.bind(SimpleNamespace(channels={"teams": (channel, None)}))
    toolkit = BackgroundTaskToolkit(service)
    key = ConversationKey(platform="teams", chat_id="19:chat", is_direct_message=True)
    token = bind_origin("teams", key)
    try:
        message = await toolkit.create_task("hold", "probe", {})
    finally:
        reset_origin(token)

    task_id = int(message.split("#", 1)[1].split(" ", 1)[0])
    listing = ""
    for _ in range(50):
        listing = await toolkit.list_running_tasks()
        if "status=running" in listing:
            break
        await asyncio.sleep(0.01)

    assert f"#{task_id}" in listing
    assert "status=running" in listing
    status = await toolkit.get_task_status(task_id)
    assert "status=running" in status
    assert service._inflight

    hold.gate.set()
    pending = set(service._inflight)
    await asyncio.wait(pending)

    status = await toolkit.get_task_status(task_id)
    assert "status=done" in status
    assert "released" in status
    assert channel.messages
    assert not service._inflight


class _Cards:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, destination: ConversationKey, message) -> str:
        self.sent.append(message)
        return "1"


class _CardResult:
    task_type = "card"
    description = "Reply with a card."

    async def run(self, task) -> OutboundMessage:
        if task.params.get("fail"):
            raise RuntimeError("model timeout")
        return OutboundMessage(text="Draft ready: X", cards=[{"type": "AdaptiveCard", "body": []}])

    async def on_failure(self, task, error: str) -> OutboundMessage:
        return OutboundMessage(
            text=f"Could not finish: {error}", cards=[{"type": "AdaptiveCard", "x": 1}]
        )


async def _create(
    service: BackgroundTaskService, platform: str, task_type: str, params: dict
) -> int:
    key = ConversationKey(platform=platform, chat_id="c1", is_direct_message=True)
    token = bind_origin(platform, key)
    try:
        record, _ = await service.create(task_type=task_type, subject="s", params=params)
    finally:
        reset_origin(token)
    assert record is not None
    await asyncio.wait(set(service._inflight))
    return record.id


@pytest.mark.asyncio
async def test_card_result_goes_to_card_channels_and_text_elsewhere(tmp_path) -> None:
    service = BackgroundTaskService(str(tmp_path / "agent.db"))
    service.register(_CardResult())
    teams, cli = _Cards(), _Cards()
    service.bind(SimpleNamespace(channels={"teams": (teams, None), "cli": (cli, None)}))

    task_id = await _create(service, "teams", "card", {})
    assert teams.sent[0].cards and not teams.sent[0].text
    stored = await service.get(task_id)
    assert stored is not None and stored.result == "Draft ready: X" and stored.attempts == 1

    await _create(service, "cli", "card", {})
    assert cli.sent[0].text == "Draft ready: X" and not cli.sent[0].cards

    task_id = await _create(service, "teams", "card", {"fail": True})
    assert teams.sent[1].cards == [{"type": "AdaptiveCard", "x": 1}]
    failed = await service.get(task_id)
    assert failed is not None and failed.status == "failed" and failed.error == "model timeout"


class _Resumable(_Hold):
    task_type = "resumable"
    resumable = True


@pytest.mark.asyncio
async def test_resume_reruns_resumable_tasks_once_and_fails_the_rest(tmp_path) -> None:
    db = str(tmp_path / "agent.db")
    store = BackgroundTaskService(db).store
    origin = {"platform": "teams", "key": {"platform": "teams", "chat_id": "c1"}}
    again = await store.insert(task_type="resumable", subject="a", params={}, origin=origin)
    await store.mark_running(again.id)
    spent = await store.insert(task_type="resumable", subject="b", params={}, origin=origin)
    await store.mark_running(spent.id)
    await store.mark_running(spent.id)
    plain = await store.insert(task_type="hold", subject="c", params={}, origin=origin)

    service = BackgroundTaskService(db)
    resumable = _Resumable()
    service.register(resumable)
    service.register(_Hold())
    channel = _Capture()
    service.bind(SimpleNamespace(channels={"teams": (channel, None)}))

    assert await service.resume() == [again.id]
    resumable.gate.set()
    await asyncio.wait(set(service._inflight))
    done = await service.get(again.id)
    assert done is not None and done.status == "done" and done.attempts == 2
    for task_id in (spent.id, plain.id):
        failed = await service.get(task_id)
        assert failed is not None and failed.status == "failed" and failed.error == INTERRUPTED
    assert sum("failed" in m for m in channel.messages) == 2


class _Flaky:
    task_type = "flaky"
    description = "Fails a few times."
    resumable = True
    max_attempts = 3
    retry_delay = 0

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.seen: list[int] = []

    async def run(self, task) -> str:
        self.seen.append(task.attempts)
        if len(self.seen) <= self.failures:
            raise ConnectionError("network down")
        return "done"


@pytest.mark.asyncio
async def test_resumable_task_retries_after_an_error(tmp_path) -> None:
    service = BackgroundTaskService(str(tmp_path / "agent.db"))
    flaky = _Flaky(failures=2)
    service.register(flaky)
    channel = _Capture()
    service.bind(SimpleNamespace(channels={"teams": (channel, None)}))

    task_id = await _create(service, "teams", "flaky", {})
    done = await service.get(task_id)
    assert done is not None and done.status == "done" and done.attempts == 3
    assert flaky.seen == [1, 2, 3]
    assert len(channel.messages) == 1 and "finished" in channel.messages[0]


@pytest.mark.asyncio
async def test_resumable_task_fails_once_attempts_run_out(tmp_path) -> None:
    service = BackgroundTaskService(str(tmp_path / "agent.db"))
    service.register(_Flaky(failures=9))
    channel = _Capture()
    service.bind(SimpleNamespace(channels={"teams": (channel, None)}))

    task_id = await _create(service, "teams", "flaky", {})
    failed = await service.get(task_id)
    assert failed is not None and failed.status == "failed" and failed.attempts == 3
    assert failed.error == "network down"
    assert len(channel.messages) == 1 and "failed" in channel.messages[0]


@pytest.mark.asyncio
async def test_resume_honours_the_handler_max_attempts(tmp_path) -> None:
    db = str(tmp_path / "agent.db")
    store = BackgroundTaskService(db).store
    origin = {"platform": "teams", "key": {"platform": "teams", "chat_id": "c1"}}
    task = await store.insert(task_type="flaky", subject="a", params={}, origin=origin)
    await store.mark_running(task.id)
    await store.mark_running(task.id)

    service = BackgroundTaskService(db)
    service.register(_Flaky(failures=0))
    service.bind(SimpleNamespace(channels={"teams": (_Capture(), None)}))
    assert await service.resume() == [task.id]
    await asyncio.wait(set(service._inflight))
    done = await service.get(task.id)
    assert done is not None and done.status == "done" and done.attempts == 3


@pytest.mark.asyncio
async def test_unknown_task_type_is_rejected(tmp_path) -> None:
    service = BackgroundTaskService(str(tmp_path / "agent.db"))
    record, message = await service.create(task_type="missing", subject="nope", params={})
    assert record is None
    assert "Unknown task type" in message


def test_task_table_follows_the_harness_prefix(tmp_path) -> None:
    plain = BackgroundTaskService(str(tmp_path / "agent.db"))
    assert plain.store is not None
    assert plain.store._model.__tablename__ == "agno_harness_background_tasks"
    named = BackgroundTaskService(
        AgnoHarnessSqliteDb(db_file=tmp_path / "prefixed.db", prefix="moradin")
    )
    assert named.store is not None
    assert named.store._model.__tablename__ == "agno_harness_background_tasks"
    assert "background_tasks" not in named._harness.models


def _store_targets(tmp_path):
    targets = [("sqlite-file", str(tmp_path / "agent.db"))]
    url = "sqlite+aiosqlite:///" + (tmp_path / "url.db").resolve().as_posix()
    targets.append(("sqlite-url", url))
    postgres = os.getenv("TEST_POSTGRES_URL")
    if postgres:
        targets.append(("postgres", postgres))
    return targets


@pytest.mark.asyncio
async def test_store_roundtrip_on_sqlite_url_and_postgres(tmp_path) -> None:
    if os.getenv("TEST_POSTGRES_URL"):
        pytest.importorskip("asyncpg")
    for name, target in _store_targets(tmp_path):
        service = BackgroundTaskService(target)
        store = service.store
        assert store is not None
        task = await store.insert(
            task_type="hold",
            subject=name,
            params={"k": "值"},
            origin={"platform": "teams", "key": {"chat_id": "c"}},
        )
        await store.mark_running(task.id)
        await store.mark_done(task.id, "ok")
        loaded = await store.get(task.id)
        assert loaded is not None
        assert loaded.status == "done" and loaded.result == "ok" and loaded.attempts == 1
        assert loaded.params == {"k": "值"}
        assert loaded.origin == {"platform": "teams", "key": {"chat_id": "c"}}
        assert await store.list_active() == []
        if name == "sqlite-file":
            from sqlalchemy import inspect

            engine = service._harness.engine
            assert engine is not None
            async with engine.connect() as conn:
                names = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert "agno_harness_background_tasks" in names
            assert "moradin_background_tasks" not in names
