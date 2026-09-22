"""Button handlers for the main agent.

Extend
------
Register one function per action_id. The handler receives a ChannelEvent.
Call app.services for the side effect. Return an OutboundMessage to reply, or None.
"""

from agno_harness import ChannelEvent


def register(relay) -> None:
    @relay.action("note.ack")
    async def acknowledge(event: ChannelEvent):
        """Acknowledge a note card button."""
        del event
        return None
