"""The wire contract's version.

Bumped whenever an event name, a payload shape or a fence syntax changes in a
way an older client would misread. Sent on ``RUN_STARTED`` and on every SSE
response, so a mismatch becomes one clear message rather than a UI that renders
half of a conversation and silently drops the rest.

Cheap to send and impossible to add retroactively: a client that has never seen
a version field cannot start requiring one.
"""

from __future__ import annotations

#: Major changes when a client must be updated; minor when it need not be.
WIRE_PROTOCOL_VERSION = "1.0"

__all__ = ["WIRE_PROTOCOL_VERSION"]
