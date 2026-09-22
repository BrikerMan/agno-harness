"""Build one Agno agent from its folder.

Extend
------
Call assemble() from app/agents/main/agent.py.
Pass specialists as the Agent objects the coordinator can call.
skills/ is a directory of <skill>/SKILL.md.
knowledge_dir is the live markdown store, normally data/knowledge/.
"""

from pathlib import Path

from agno.agent import Agent

from agno_harness import MarkdownKnowledge, SkillManager, SubAgentToolkit
from app.settings import model


def assemble(
    *,
    name: str,
    description: str,
    instructions_path: Path,
    catalog,
    skills_dir: Path,
    knowledge_dir: Path,
    tools: list,
    specialists: list | None = None,
    db=None,
) -> Agent:
    extra_tools: list = []
    text = instructions_path.read_text(encoding="utf-8").strip()
    if skills_dir.is_dir():
        manager = SkillManager(catalog=catalog, sources=[skills_dir], strict=True)
        extra_tools.append(manager.create_load_skill_tool())
        skill_prompt = manager.to_roster_prompt()
        if skill_prompt:
            text = f"{text}\n\n{skill_prompt}"
    if specialists:
        extra_tools.append(SubAgentToolkit(agents=specialists))
    kwargs = {
        "name": name,
        "model": model(),
        "instructions": text,
        "tools": [*tools, *extra_tools],
        "telemetry": False,
        "markdown": True,
    }
    if description:
        kwargs["description"] = description
    if db is not None:
        kwargs["db"] = db
        kwargs["add_history_to_context"] = True
        kwargs["num_history_runs"] = 100
    if knowledge_dir.is_dir():
        library = MarkdownKnowledge(knowledge_dir)
        kwargs["knowledge_retriever"] = library.search
        kwargs["search_knowledge"] = True
    return Agent(**kwargs)
