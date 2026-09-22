"""Terminal channel.

Extend
------
mount() adds CLIChannel and returns it so app.cli can run the read loop.
The FastAPI process does not read stdin.
"""

import logging

from agno_harness import CLIChannel

log = logging.getLogger(__name__)


def mount(relay, app):
    del app
    if relay is None:
        message = "CLI channel is enabled but no RelayApp was provided."
        log.error(message)
        raise RuntimeError(message)
    channel = CLIChannel()
    relay.add_channel(channel)
    return channel
