"""Coordinator agent.

Extend
------
Edit instructions.md for voice and when to delegate.
Add toolkits under tools/ and export them from tools/__init__.py.
Pass extra specialists into assemble() next to Researcher and AgentBuilder.
This agent is the one on AgentRuntime.
"""

from pathlib import Path

from app.agents.agent_builder import build as build_agent_builder
from app.agents.assemble import assemble
from app.agents.main.cards import CARD_CATALOG
from app.agents.main.tools import TOOLS
from app.agents.researcher.agent import build as build_researcher
from app.paths import KNOWLEDGE

NAME = "Coordinator"
_DIR = Path(__file__).resolve().parent


def build(db):
    return assemble(
        name=NAME,
        description="",
        instructions_path=_DIR / "instructions.md",
        catalog=CARD_CATALOG,
        skills_dir=_DIR / "skills",
        knowledge_dir=KNOWLEDGE,
        tools=list(TOOLS),
        specialists=[build_researcher(), build_agent_builder()],
        db=db,
    )
