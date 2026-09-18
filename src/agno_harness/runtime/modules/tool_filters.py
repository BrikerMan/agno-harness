"""The tool-filter chain, as a module.

Filtering used to happen inside the chunk converter, which forced a second
concern into the same loop: the sub-agent tracker had to see tool events
*before* they were filtered, because the tool that delegates is usually a hidden
one. That was solved with an extra argument threaded through the converter.

As a module it is solved by registration order instead. The sub-agent module
runs first and sees every tool call; this one runs next and drops what the
client should not see. The dependency is visible at the assembly site rather
than implied by a parameter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from ag_ui.core import BaseEvent

from ...core.filters import collect_hidden_tool_names
from ...core.types import ToolFilter
from ..module import Module
from ..scope import RunScope


class ToolFilterModule(Module):
    """Runs each event through the filter chain; the chain stops at a drop.

    Non-tool events pass through untouched — every built-in filter inspects the
    event type first — so registering this module does not put reasoning or
    generative-UI frames at risk.
    """

    name: str = "tool_filters"
    namespace: str | None = None

    def __init__(self, filters: Iterable[ToolFilter] = ()) -> None:
        self.filters: list[ToolFilter] = list(filters)

    def add(self, tool_filter: ToolFilter) -> ToolFilterModule:
        self.filters.append(tool_filter)
        return self

    @property
    def hidden_tool_names(self) -> set[str]:
        """Names replay must keep hidden, so history matches what streamed."""
        return collect_hidden_tool_names(self.filters)

    async def stage(self, event: BaseEvent, run: RunScope) -> AsyncIterator[BaseEvent]:
        current: BaseEvent | None = event
        for tool_filter in self.filters:
            if current is None:
                return
            current = tool_filter(current)
        if current is not None:
            yield current


__all__ = ["ToolFilterModule"]
