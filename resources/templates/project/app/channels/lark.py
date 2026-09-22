"""Lark channel. WebSocket, started with the relay lifespan.

Extend
------
If the channel is enabled, missing settings must log an error and raise.
Do not return when configuration is missing.
"""

import logging

from agno_harness import LarkChannel, RelayConfig

log = logging.getLogger(__name__)


def mount(relay, app) -> None:
    del app
    missing: list[str] = []
    if not RelayConfig.lark_app_id():
        missing.append("AGNO_HARNESS_LARK_APP_ID")
    if not RelayConfig.lark_app_secret():
        missing.append("AGNO_HARNESS_LARK_APP_SECRET")
    if missing:
        message = "Lark channel is enabled but missing " + " and ".join(missing) + "."
        log.error(message)
        raise RuntimeError(message)
    relay.add_channel(LarkChannel(use_websocket=True))
