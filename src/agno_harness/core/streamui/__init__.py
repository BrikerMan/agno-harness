"""StreamUI — declarative cards streamed inside the agent's answer.

Not A2UI. Real A2UI (v0.9.1 / v1.0) wants an adjacency-list component tree, a
separate data model, JSON Pointer bindings and four classes of catalog and
surface lifecycle message. For "show me a few cards" that is a great deal of
protocol, and taking a subset of it would produce something both incompatible
with A2UI and more complicated than the job needs. So this is its own small
protocol and makes no claim of compatibility.

What it is: a fenced block in the text stream whose header names a schema, and
whose body is either JSON items or raw text. Four events come out —
``ui.block.start``, ``ui.item``, ``ui.text``, ``ui.block.end``.

Deliberate non-goals, recorded here so they do not get relitigated:

*No nested cards.* Nesting is what pushed A2UI to an adjacency list.
*No interactive controls.* Cards display; interaction goes through AG-UI's
existing human-in-the-loop path.
*No update.* Walking through movie lists, repository details, headline numbers,
weather and long-running progress, not one of them genuinely needed "resend the
same id to replace it" — progress reads better as appended steps than as a bar
edited in place. Making every renderer handle "this card already exists" is a
real cost for a hypothetical benefit. ``id`` stays a pure identity marker for
React keys and replay de-duplication. Should the need appear, same-id
replacement is a backwards-compatible addition that changes no wire format.
"""

from .parser import (
    EVENT_BLOCK_END,
    EVENT_BLOCK_START,
    EVENT_ITEM,
    EVENT_TEXT,
    FENCE_TAG,
    StreamUIBlock,
    StreamUIEvent,
    StreamUIFenceParser,
    StreamUIItem,
    blocks_to_payload,
    parse_streamui_text,
)
from .schema import (
    BlockSchema,
    BodyMode,
    CardCatalog,
    CardSchema,
    CatalogConflict,
    ItemSchema,
    MediaUrl,
    Resolver,
)

__all__ = [
    "EVENT_BLOCK_END",
    "EVENT_BLOCK_START",
    "EVENT_ITEM",
    "EVENT_TEXT",
    "FENCE_TAG",
    "BlockSchema",
    "BodyMode",
    "CardCatalog",
    "CardSchema",
    "CatalogConflict",
    "ItemSchema",
    "MediaUrl",
    "Resolver",
    "StreamUIBlock",
    "StreamUIEvent",
    "StreamUIFenceParser",
    "StreamUIItem",
    "blocks_to_payload",
    "parse_streamui_text",
]
