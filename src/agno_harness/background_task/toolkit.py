"""Tools the agent calls to start a task and return before it finishes."""

from __future__ import annotations

import json
from typing import Any

from agno.tools import Toolkit

from .service import BackgroundTaskService


class BackgroundTaskToolkit(Toolkit):
    def __init__(self, service: BackgroundTaskService) -> None:
        self.service = service
        super().__init__(
            name="background_task",
            tools=[self.list_running_tasks, self.create_task, self.get_task_status],
            instructions=service.catalog(),
            add_instructions=True,
        )

    async def list_running_tasks(self) -> str:
        """List background tasks that are queued or running. Call this before creating one."""
        active = await self.service.list_running()
        if not active:
            return "No background tasks are running."
        lines = []
        for task in active:
            params = ", ".join(f"{key}={value}" for key, value in task.params.items())
            lines.append(
                f"- #{task.id} {task.task_type} subject={task.subject} "
                f"{params or 'no params'} status={task.status}"
            )
        return "\n".join(lines)

    async def create_task(
        self,
        task_type: str,
        subject: str,
        params: dict[str, Any] | str | None = None,
    ) -> str:
        """Create a background task and return immediately.

        Args:
            task_type: A type from the catalog.
            subject: Short name of the work, stable if the same job is asked again.
            params: JSON object of arguments for that task type.

        Returns:
            The new task id, or why it was not created.
        """
        parsed = _params(params)
        if isinstance(parsed, str):
            return parsed
        _record, message = await self.service.create(
            task_type=task_type,
            subject=subject,
            params=parsed,
        )
        return message

    async def get_task_status(self, task_id: int) -> str:
        """Return the status of one background task."""
        task = await self.service.get(int(task_id))
        if task is None:
            return f"No background task #{task_id}."
        detail = task.result or task.error or ""
        suffix = f" {detail}" if detail else ""
        return (
            f"Task #{task.id}: {task.task_type} subject={task.subject} "
            f"status={task.status}.{suffix}"
        )


def _params(params: dict[str, Any] | str | None) -> dict[str, Any] | str:
    if params is None:
        return {}
    if isinstance(params, str):
        text = params.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            return f"Could not create the task: params is not valid JSON ({exc})."
        if not isinstance(loaded, dict):
            return "Could not create the task: params must be a JSON object."
        return loaded
    if not isinstance(params, dict):
        return "Could not create the task: params must be a JSON object."
    return params
