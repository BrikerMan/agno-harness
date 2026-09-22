"""Minimal note card.

Extend
------
Subclass BlockSchema. Set schema_name. Put facts in resolve().
Put Teams and Lark fragments in render_teams and render_lark.
"""

from typing import Any, ClassVar

from agno_harness import BlockSchema


class NoteCard(BlockSchema):
    schema_name: ClassVar[str] = "note"
    body: ClassVar[str] = "text"
    include_in_system_prompt: ClassVar[bool] = False

    async def resolve(self, ctx: Any = None) -> dict[str, Any]:
        return {"title": "Note"}
