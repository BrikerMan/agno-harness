"""RunScope — everything one run owns.

The runtime object is shared by every run a process serves, so nothing that
belongs to a single run may live on it. That sounds obvious and was quietly
violated: protocol repairs were kept in one list on the bridge, so two
overlapping runs reported each other's.

A scope is created per run and threaded through the translator, the runner and
the hooks. Its mutable fields are deliberately mutable — a pre-run hook is
expected to reach in and set ``user_id``, seed ``session_state`` or add a keyword
argument for ``agent.arun()``, which is the difference between a hook that can
only announce things and one that can change them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from agno.os.interfaces.agui.state import StreamState

from .inspector import RunInspector
from .state import StateTracker

_CURRENT_SCOPE: ContextVar[RunScope | None] = ContextVar("better_agno_current_scope", default=None)


def current_scope() -> RunScope | None:
    """Return the active RunScope for the current async task, if any."""
    return _CURRENT_SCOPE.get()


@contextmanager
def bind_scope(scope: RunScope | None) -> Iterator[RunScope | None]:
    """Establish a RunScope as ambient context for the duration of a block."""
    token = _CURRENT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        try:
            _CURRENT_SCOPE.reset(token)
        except ValueError:
            if _CURRENT_SCOPE.get() is scope:
                _CURRENT_SCOPE.set(None)


@dataclass
class RunScope:
    """The identity, state and scratch space of one run."""

    thread_id: str
    run_id: str
    stream_state: StreamState
    state_tracker: StateTracker
    inspector: RunInspector
    user_id: str | None = None

    # The latest user turn, copied off the AG-UI request at start. Frames never
    # carry the prompt, so anything that needs it (tracing, a mid-run reload)
    # has to read it from here rather than from the stream.
    user_text: str = ""

    # Client ``forwardedProps`` from the AG-UI request. Opaque to the runtime;
    # pre-run hooks read it to tune the model, inject flags, and so on.
    forwarded_props: Any = None

    # Extra keyword arguments for ``agent.arun()``. Pre-run hooks may add to it;
    # the runner merges it into the call.
    run_kwargs: dict[str, Any] = field(default_factory=dict)

    # Agno's per-run context, set by the runner once the run is under way.
    run_context: Any = None

    # Entered around each step of the agent's own execution — see enter_step().
    step_context: Callable[[], AbstractContextManager[Any]] | None = None

    # Set when the client went away mid-run. The run has no terminal event in
    # that case, so anything recording an outcome has to read it from here.
    disconnected: bool = False

    # Per-run scratch space for modules, keyed by module name so two modules
    # cannot collide on a shared key.
    data: dict[str, Any] = field(default_factory=dict)

    # Multi-dimensional metadata (platform, tags, channel ids, tenant) for observability
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_state(self) -> dict[str, Any] | None:
        """The shared-state document, or ``None`` when state sync is off."""
        return self.state_tracker.state

    def enable_session_state(self, initial: dict[str, Any] | None = None) -> dict[str, Any]:
        """Turn on shared state for this run and return the live document.

        A client that sends no ``state`` gets no state sync, which is the right
        default. A pre-run hook that wants to seed the document anyway — a
        feature flag, the user's plan — calls this and mutates what it returns.
        """
        return self.state_tracker.enable(initial)

    def module_data(self, name: str) -> dict[str, Any]:
        """A module's private corner of this run's scratch space."""
        return self.data.setdefault(name, {})

    def enter_step(self) -> AbstractContextManager[Any]:
        """Wrap one step of the agent's execution — the wait for its next chunk.

        A caller that needs ambient state established while the *agent* runs,
        but not while the rest of the pipeline does, sets ``step_context`` and
        gets it entered around each of those waits. Tracing is the case that
        exists today: it makes the run's span current so spans opened by the
        agent's own instrumentation nest inside it.

        Per step rather than once around the whole stream, because the runner
        is an async generator and Python has no per-generator context: state
        established inside one leaks into whoever is consuming it, and two
        concurrent runs end up nested in each other. Entering and leaving
        around a single ``await`` keeps it inside the task that owns the run.
        """
        return self.step_context() if self.step_context is not None else nullcontext()


__all__ = ["RunScope", "bind_scope", "current_scope"]
