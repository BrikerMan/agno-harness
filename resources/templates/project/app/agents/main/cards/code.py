"""Source snippet. Monospace code block on a shaded container."""

from typing import ClassVar

from agno_harness import BlockSchema


class CodeCard(BlockSchema):
    """A source snippet. Body is the code, without a markdown fence."""

    schema_name: ClassVar[str] = "code"
    body: ClassVar[str] = "text"
    teams_container_style: ClassVar[str] = "emphasis"
    teams_code_block: ClassVar[bool] = True
