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

    - ``resumable: bool``: run the task again after a restart or an error instead of failing it, while
      attempts remain. ``run`` sees ``task.attempts`` (1 on the first run) and should pick up saved work.
    - ``max_attempts: int``: runs counting the first (default 2).
    - ``retry_delay: float``: seconds before a retry after an error, times the attempts so far (default 30).
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
