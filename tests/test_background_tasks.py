import asyncio
from types import SimpleNamespace

import pytest

from agno_harness.background_task.context import bind_origin, reset_origin
from agno_harness.background_task.handlers import SleepHandler
from agno_harness.background_task.service import INTERRUPTED, BackgroundTaskService
from agno_harness.background_task.toolkit import BackgroundTaskToolkit
from agno_harness.core.channel import OutboundMessage
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


@pytest.mark.asyncio
async def test_unknown_task_type_is_rejected(tmp_path) -> None:
    service = BackgroundTaskService(str(tmp_path / "agent.db"))
    record, message = await service.create(task_type="missing", subject="nope", params={})
    assert record is None
    assert "Unknown task type" in message
