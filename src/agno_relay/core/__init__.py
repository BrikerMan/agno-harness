"""core — the protocol layer.

Nothing here knows about Agno or about HTTP. The sequencer enforces AG-UI's
invariants, the fence parser lifts generative-UI blocks out of text, the filters
rewrite tool events, and the type aliases describe the extension points. All of
it operates on AG-UI events alone.

That restriction is load-bearing rather than aesthetic, and ``test_layering.py``
enforces it: it is what lets the sequencer be tested against hand-written event
sequences with no agent in sight, and what would let a non-Agno runtime reuse
this layer unchanged.
"""

from .debug import EVENT_DEBUG_SUMMARY, DebugTap
from .filters import (
    HideToolFilter,
    RedactFilter,
    TransformResultFilter,
    collect_hidden_tool_names,
)
from .log import (
    CompactionPolicy,
    Frame,
    FrameKind,
    NoCompaction,
    RunEventLog,
    RunEventStream,
    RunRecord,
    RunStatus,
    coalesce_events,
    select_frames,
)
from .protocol import WIRE_PROTOCOL_VERSION
from .sequencer import (
    EventSequencer,
    ProtocolViolation,
    ProtocolViolationError,
    SequencerMode,
)
from .streamui import (
    EVENT_BLOCK_END,
    EVENT_BLOCK_START,
    EVENT_ITEM,
    EVENT_TEXT,
    FENCE_TAG,
    BlockSchema,
    BodyMode,
    CardCatalog,
    CardSchema,
    CatalogConflict,
    ItemSchema,
    MediaUrl,
    Resolver,
    StreamUIBlock,
    StreamUIEvent,
    StreamUIFenceParser,
    StreamUIItem,
    blocks_to_payload,
    parse_streamui_text,
)
from .types import ToolFilter

__all__ = [
    "WIRE_PROTOCOL_VERSION",
    "EVENT_BLOCK_END",
    "EVENT_BLOCK_START",
    "EVENT_DEBUG_SUMMARY",
    "EVENT_ITEM",
    "EVENT_TEXT",
    "FENCE_TAG",
    "BlockSchema",
    "BodyMode",
    "CardCatalog",
    "CardSchema",
    "CatalogConflict",
    "CompactionPolicy",
    "DebugTap",
    "EventSequencer",
    "Frame",
    "FrameKind",
    "HideToolFilter",
    "ItemSchema",
    "MediaUrl",
    "NoCompaction",
    "ProtocolViolation",
    "ProtocolViolationError",
    "RedactFilter",
    "Resolver",
    "RunEventLog",
    "RunEventStream",
    "RunRecord",
    "RunStatus",
    "SequencerMode",
    "StreamUIBlock",
    "StreamUIEvent",
    "StreamUIFenceParser",
    "StreamUIItem",
    "ToolFilter",
    "TransformResultFilter",
    "blocks_to_payload",
    "coalesce_events",
    "collect_hidden_tool_names",
    "parse_streamui_text",
    "select_frames",
]
