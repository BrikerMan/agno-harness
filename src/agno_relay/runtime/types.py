"""Protocols for extension points that see more than AG-UI events.

Split from :mod:`agno_relay.core.types` for one reason: these
signatures name Agno's chunk types or the runtime's own
:class:`~agno_relay.runtime.scope.RunScope`. Everything that can be
expressed in AG-UI events alone stays in ``core``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Any, Protocol

from ag_ui.core import BaseEvent

if TYPE_CHECKING:
    from agno.os.interfaces.agui.state import StreamState

    from .scope import RunScope


class EventParser(Protocol):
    """Supplements the official Agno handler for one chunk category.

    Runs *after* the official handler (see ``EventTranslator.convert``) and
    yields additional AG-UI events. A parser never replaces official output — it
    only adds to it.
    """

    def __call__(self, chunk: Any, state: StreamState) -> Iterable[BaseEvent]: ...


class PreRunHook(Protocol):
    """Async generator invoked with the run's scope before the agent starts.

    The scope is deliberately mutable. A hook may set ``scope.user_id`` from an
    entitlement lookup, call ``scope.enable_session_state()`` to seed the shared
    document, or add to ``scope.run_kwargs`` — all of which take effect because
    the runner builds Agno's ``RunContext`` after the hooks have run.

    It may also perform side effects (persisting a billing row, say) and yield
    events. Yielded events are emitted immediately after ``RUN_STARTED``; the
    sequencer guarantees the ordering even though the hook runs first.
    """

    def __call__(self, scope: RunScope) -> AsyncIterator[BaseEvent]: ...


class PostRunHook(Protocol):
    """Async generator invoked once the agent is done, before the run closes.

    ``completion`` is Agno's terminal chunk, or ``None`` when the run failed
    before producing one; ``error`` is the exception that ended the run, if any.
    Events yielded here land inside the run, ahead of ``RUN_FINISHED``.
    """

    def __call__(
        self,
        scope: RunScope,
        *,
        completion: Any,
        error: BaseException | None,
    ) -> AsyncIterator[BaseEvent]: ...


__all__ = ["EventParser", "PostRunHook", "PreRunHook"]
