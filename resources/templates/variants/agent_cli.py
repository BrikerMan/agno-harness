"""Process entry for the terminal.

This file calls cli.mount directly. It does not call mount_all and does not start FastAPI.
app.main.create_app remains for a later HTTP process.
"""

import asyncio

from app.channels.cli import mount
from app.runtime_factory import build_relay

from agno_harness import setup_relay_logging


async def _run() -> None:
    setup_relay_logging(logger_names=["agno_harness", "ag_ui", "app"])
    relay = build_relay()
    channel = mount(relay, None)
    await relay.start()
    try:
        await channel.run_interactive_loop()
    finally:
        await relay.stop()


if __name__ == "__main__":
    asyncio.run(_run())
