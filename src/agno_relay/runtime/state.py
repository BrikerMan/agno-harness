"""Shared state sync — STATE_SNAPSHOT and JSON Patch STATE_DELTA.

AG-UI lets the agent and the client share one JSON document. The client seeds it
on ``RunAgentInput.state``, the agent mutates it during a run, and the changes
travel back as RFC 6902 patches so the client never re-renders from scratch.

Agno already implements the delta arithmetic on ``StreamState`` — it diffs
``run_state`` against the previous snapshot after every tool call. What it does
not do is wire the client's state *in*, which is what :class:`StateTracker`
handles. The one subtlety is object identity: the dict handed to
``StreamState.run_state`` must be the very same dict the agent's
``RunContext.session_state`` points at, otherwise tool mutations are invisible
to the differ.
"""

from __future__ import annotations

import copy
from typing import Any

from ag_ui.core import BaseEvent, EventType, StateSnapshotEvent
from agno.os.interfaces.agui.state import StreamState


class StateTracker:
    """Owns the shared-state document for one run.

    Parameters
    ----------
    initial:
        The client's state from ``RunAgentInput.state``. ``None`` disables state
        sync entirely, which is the right default for agents that do not use it
        (no snapshot, no deltas, no overhead).
    """

    def __init__(self, initial: dict[str, Any] | None) -> None:
        # Deep-copied so a caller reusing the input object cannot mutate our
        # baseline out from under the differ.
        self._state: dict[str, Any] | None = copy.deepcopy(initial) if initial is not None else None

    @property
    def enabled(self) -> bool:
        return self._state is not None

    @property
    def state(self) -> dict[str, Any] | None:
        """The live document. Hand this to ``RunContext(session_state=...)``."""
        return self._state

    def enable(self, initial: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start state sync for a run whose client did not ask for it.

        Idempotent, and never discards what is already there: a pre-run hook
        seeding defaults must not wipe the document the client sent.
        """
        if self._state is None:
            self._state = {}
        if initial:
            for key, value in initial.items():
                self._state.setdefault(key, copy.deepcopy(value))
        return self._state

    def bind(self, stream_state: StreamState) -> None:
        """Attach the document to Agno's per-stream state, sharing identity."""
        if self._state is None:
            return
        stream_state.run_state = self._state
        stream_state.set_state_snapshot(self._state)

    def initial_events(self) -> list[BaseEvent]:
        """The opening ``STATE_SNAPSHOT``, so the client and agent start aligned."""
        if self._state is None:
            return []
        return [
            StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=copy.deepcopy(self._state))
        ]

    def sync_from_run_context(self, run_context: Any) -> None:
        """Re-point at ``run_context.session_state`` if Agno swapped the object.

        Agno normally mutates the dict in place, but some code paths assign a
        fresh one. When that happens the differ would compare our stale object
        forever, so we follow the reassignment.
        """
        if self._state is None or run_context is None:
            return
        current = getattr(run_context, "session_state", None)
        if isinstance(current, dict) and current is not self._state:
            self._state.clear()
            self._state.update(current)


__all__ = ["StateTracker"]
