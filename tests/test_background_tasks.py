import asyncio
from types import SimpleNamespace

import pytest

from agno_harness.background_task.context import bind_origin, reset_origin
from agno_harness.background_task.handlers import SleepHandler
from agno_harness.background_task.service import BackgroundTaskService
from agno_harness.background_task.toolkit import BackgroundTaskToolkit
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


@pytest.mark.asyncio
async def test_unknown_task_type_is_rejected(tmp_path) -> None:
    service = BackgroundTaskService(str(tmp_path / "agent.db"))
    record, message = await service.create(task_type="missing", subject="nope", params={})
    assert record is None
    assert "Unknown task type" in message
