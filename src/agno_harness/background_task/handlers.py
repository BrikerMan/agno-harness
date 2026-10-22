"""Built-in handlers. A project registers more on the service."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from ..core.channel import OutboundMessage
from .store import TaskRecord


class BackgroundHandler(Protocol):
    """A task type.

    ``run`` returns the text posted back to the chat, or an ``OutboundMessage``: its cards go to
    channels that render cards (Teams, Lark) and its text everywhere else and into the task row.

    Optional attributes, read with ``getattr``:

    - ``resumable: bool``: after a restart, run an unfinished task again (once) instead of failing it.
    - ``async def on_failure(task, error) -> OutboundMessage | str | None``: what to post when the
      task fails or a restart interrupts it. ``None`` posts the default failure text.
    """

    task_type: str
    description: str

    async def run(self, task: TaskRecord) -> str | OutboundMessage: ...


class SleepHandler:
    """Wait, then report back. Useful as a probe that the reply path works."""

    task_type = "sleep"
    description = 'Wait, then report back. params: {"seconds": N}.'

    async def run(self, task: TaskRecord) -> str:
        raw: Any = (task.params or {}).get("seconds", 1)
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            seconds = 1
        seconds = max(0, min(seconds, 120))
        await asyncio.sleep(seconds)
        return f"Finished waiting {seconds} seconds."
