"""Background tasks stored in data/agent.db.

create_task writes a row and returns. The run continues on the event loop.
list_running_tasks and get_task_status read that row.
"""

from agno_harness.background_task import BackgroundTaskService, BackgroundTaskToolkit, SleepHandler
from app.paths import AGENT_DB

service = BackgroundTaskService(str(AGENT_DB))
service.register(SleepHandler())
toolkit = BackgroundTaskToolkit(service)


def bind(relay) -> None:
    """Let a finished task post its result back to the chat that asked."""
    service.bind(relay)
