"""Terminal entry. Same factories as the FastAPI process, CLI channel only.

Extend
------
Keep this file as a thin loop. Business changes belong in app/agents/.
"""

import asyncio

from agno_harness import setup_relay_logging
from app.channels.cli import mount
from app.runtime_factory import build_relay


async def run() -> None:
    setup_relay_logging(logger_names=["agno_harness", "ag_ui", "app"])
    relay = build_relay()
    channel = mount(relay, None)
    await relay.start()
    try:
        await channel.run_interactive_loop()
    finally:
        await relay.stop()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
