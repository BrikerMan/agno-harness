"""Protocols for the extension points that deal only in AG-UI events.

Every extension point the toolbox exposes is a plain callable, so you can
register a function, a lambda, or a stateful class instance interchangeably.

The parser and hook protocols are not here but in
:mod:`agno_harness.runtime.types`, because their signatures name a raw
Agno chunk, Agno's translator state, or the runtime's own ``RunScope``. Naming
those types is enough to make a module Agno-dependent, and the whole point of
``core`` is that it is not.
"""

from __future__ import annotations

from typing import Protocol

from ag_ui.core import BaseEvent


class ToolFilter(Protocol):
    """Drops (``None``) or rewrites a tool-call event.

    Filters are chained in registration order and the chain stops at the first
    drop.
    """

    def __call__(self, event: BaseEvent) -> BaseEvent | None: ...


__all__ = ["ToolFilter"]
