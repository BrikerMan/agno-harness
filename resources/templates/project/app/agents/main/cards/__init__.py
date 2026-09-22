"""Cards the main agent can emit.

Extend
------
Add one BlockSchema per file. Register the class in CARD_CATALOG.
schema_name is the id a skill frontmatter cards: list may use.
A skill that names an unknown schema fails at startup.
"""

from agno_harness import CardCatalog
from app.agents.main.cards.note import NoteCard

CARD_CATALOG = CardCatalog([NoteCard])
