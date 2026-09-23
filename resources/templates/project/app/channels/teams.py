"""Teams channel. Webhook is POST /api/v1/channels/teams/messages.

Extend
------
Copy this file for a channel that needs credentials.
If the channel is enabled, missing settings must log an error and raise.
Do not return when configuration is missing.
"""

import logging

from agno_harness import TeamsChannel, TeamsCredentialsError

log = logging.getLogger(__name__)


def mount(relay, app) -> None:
    del app
    channel = TeamsChannel()
    try:
        channel.require_credentials()
    except TeamsCredentialsError as exc:
        log.error("%s", exc)
        raise
    relay.add_channel(channel)
