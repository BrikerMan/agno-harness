"""Sample specialist the coordinator can call.

Extend
------
Replace this module with a real helper, or stop passing build() into
assemble() from app/agents/main/agent.py.
This agent does not know your product.
"""

from agno.agent import Agent

from app.settings import model

NAME = "AgentBuilder"


def build() -> Agent:
    return Agent(
        name=NAME,
        description="Sample specialist. Replace this module with a real helper.",
        model=model(),
        instructions=(
            "You are a sample specialist. "
            "Tell the coordinator this agent does not know the product. "
            "Do not invent product facts."
        ),
        telemetry=False,
        markdown=True,
    )
