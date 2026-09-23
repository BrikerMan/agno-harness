"""Web research specialist.

Extend
------
Edit instructions.md for how search results are reported.
Add toolkits under tools/ and export them from tools/__init__.py.
The coordinator delegates here when the knowledge notes do not contain the fact.
This agent is not mounted on a channel.
"""

from pathlib import Path

from app.agents.assemble import assemble
from app.agents.researcher.tools import TOOLS

NAME = "Researcher"
_DIR = Path(__file__).resolve().parent


def build():
    return assemble(
        name=NAME,
        description=(
            "Search the public web and return titles, URLs, and short snippets. Cite every URL."
        ),
        instructions_path=_DIR / "instructions.md",
        catalog=None,
        skills_dir=_DIR / "skills",
        knowledge_dir=_DIR / "knowledge",
        tools=list(TOOLS),
    )
