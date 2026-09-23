"""Time and event. Accent title and a FactSet."""

from typing import Any, ClassVar

from agno_harness import BlockSchema
from agno_harness.cards import fragment_text, teams_event_card


class EventCard(BlockSchema):
    """A time and what happened. Body lines may start with Time: and Event:."""

    schema_name: ClassVar[str] = "event"
    body: ClassVar[str] = "text"

    def render_teams_block(self, rendered_items: list[dict[str, Any]]) -> dict[str, Any]:
        return teams_event_card(fragment_text(rendered_items))
