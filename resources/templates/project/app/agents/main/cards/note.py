"""Prose note. Emphasis container, wrapped text."""

from typing import Any, ClassVar

from agno_harness import BlockSchema


class NoteCard(BlockSchema):
    """A short prose note. Body is the note text."""

    schema_name: ClassVar[str] = "note"
    body: ClassVar[str] = "text"
    teams_container_style: ClassVar[str] = "emphasis"

    async def resolve(self, ctx: Any = None) -> dict[str, Any]:
        return {"title": "Note"}
