"""Placeholder specialist.

MUST CHANGE BEFORE PRODUCTION.
Replace this module with a real helper, or stop passing build() into the
main agent, before this process serves anyone but you.
"""

from agno.agent import Agent

from app.settings import model

NAME = "AgentBuilder"


def build() -> Agent:
    # MUST CHANGE BEFORE PRODUCTION.
    return Agent(
        name=NAME,
        description="Placeholder. Replace this specialist before production.",
        model=model(),
        instructions=(
            "You are a placeholder specialist. "
            "Tell the coordinator this agent must be replaced before production. "
            "Do not invent product facts."
        ),
        telemetry=False,
        markdown=True,
    )
