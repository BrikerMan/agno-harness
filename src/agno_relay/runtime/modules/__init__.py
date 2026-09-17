"""runtime.modules — optional capabilities that hang off the pipeline.

One feature, one file. Sub-agent streaming used to be spread across three
modules — the bus here, the bookkeeping on the bridge, the read-back in replay —
with nothing forcing the three to agree.

Registration order is pipeline order, and the runtime's default order encodes
real dependencies: sub-agents before tool filters (so the delegating tool is
seen before it is hidden), tool filters before generative UI (so a hidden tool's
output never reaches the fence parser).
"""

from .custom_events import CustomEventsModule
from .streamui import StreamUIModule, UIItem, UISignal, emit_item, emit_text, ui_block
from .subagent import (
    EVENT_SUBAGENT_END,
    EVENT_SUBAGENT_START,
    SubAgentBus,
    SubAgentModule,
    SubAgentRecord,
    SubAgentTracker,
    SubStream,
    substream,
)
from .tool_filters import ToolFilterModule

__all__ = [
    "EVENT_SUBAGENT_END",
    "EVENT_SUBAGENT_START",
    "CustomEventsModule",
    "SubAgentBus",
    "SubAgentModule",
    "SubAgentRecord",
    "SubAgentTracker",
    "StreamUIModule",
    "SubStream",
    "ToolFilterModule",
    "UIItem",
    "UISignal",
    "emit_item",
    "emit_text",
    "substream",
    "ui_block",
]
