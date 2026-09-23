"""Specialist names stay distinct from the coordinator.

Extend
------
When you add app/agents/<name>/, assert its NAME here.
"""


def test_specialist_name() -> None:
    from app.agents.agent_builder import NAME as builder_name
    from app.agents.main.agent import NAME as main_name
    from app.agents.researcher.agent import NAME as researcher_name

    assert main_name == "Coordinator"
    assert researcher_name == "Researcher"
    assert builder_name == "AgentBuilder"
    assert len({main_name, researcher_name, builder_name}) == 3
