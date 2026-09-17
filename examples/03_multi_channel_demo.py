"""Multi-channel production demo combining CLI + Web in a single RelayApp.

Run with:
    uv run python examples/03_multi_channel_demo.py
"""

import asyncio
from typing import Any
from fastapi import FastAPI
import uvicorn

from agno_relay import (
    BlockSchema,
    CardCatalog,
    CLIChannel,
    ItemSchema,
    RelayApp,
    SQLiteSink,
    WebChannel,
)


class TaskItem(ItemSchema):
    schema_name = "task"
    id: int
    title: str

    def render_cli(self, resolved: dict[str, Any]) -> str:
        return f"• [bold green]Task #{self.id}[/bold green]: {self.title}"


class TaskBlock(BlockSchema):
    schema_name = "task-list"
    body = "items"
    item = TaskItem
    title: str | None = None


catalog = CardCatalog([TaskBlock])


class OfflineAssistant:
    def __init__(self, instructions: str = ""):
        self.instructions = instructions

    async def arun(self, *args, **kwargs):
        from agno.run.agent import RunContentEvent, RunCompletedEvent
        yield RunContentEvent(
            content=(
                "Here are your upcoming tasks:\n\n"
                "```stream-ui {\"schema\": \"task-list\", \"title\": \"Sprint Backlog\"}\n"
                "{\"id\": 1, \"title\": \"Review agno-relay specification\"}\n"
                "{\"id\": 2, \"title\": \"Deploy Teams and Lark bot adapters\"}\n"
                "```\n\n"
                "Let me know if you want to reschedule any of them."
            )
        )
        yield RunCompletedEvent()


# 1. Build RelayApp Chassis
app = RelayApp(OfflineAssistant(), card_catalog=catalog)

# 2. Add multiple channels simultaneously
cli_channel = CLIChannel()
app.add_channel(cli_channel)

# 3. Add SQLite message persistence sink
app.add_sink(SQLiteSink(db_path="data/multi_channel_audit.db"))


async def main():
    print("Multi-channel RelayApp demo starting...")
    await app.start()
    try:
        # Run CLI loop
        await cli_channel.run_interactive_loop(chat_id="demo_multi", sender_id="developer")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
