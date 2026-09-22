"""Placeholder specialist stays distinct from the coordinator.

Extend
------
When you add app/agents/<name>.py, assert its NAME here.
"""


def test_specialist_name() -> None:
    from app.agents.agent_builder import NAME as builder_name
    from app.agents.main.agent import NAME as main_name

    assert main_name == "Coordinator"
    assert builder_name == "AgentBuilder"
    assert main_name != builder_name
