"""Built-in handlers. A project registers more on the service."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from .store import TaskRecord


class BackgroundHandler(Protocol):
    task_type: str
    description: str

    async def run(self, task: TaskRecord) -> str: ...


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
