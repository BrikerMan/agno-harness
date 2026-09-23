# 07. Markdown notes

`agno-harness init` always writes notes, one skill, and one sample specialist, `AgentBuilder`. `--channel` only chooses how the process starts. `AgentBuilder` does not know your product. The resolvers in `app/identity.py` are local defaults. Replace both before this process serves anyone but you.

Seed notes are `app/knowledge/*.md`. Startup copies a seed into `data/knowledge/` only when that file is missing. The agent searches `data/knowledge/`. The database is `data/agent.db`. `data/` is gitignored. Edit a file under `data/knowledge/` and the next search reads it.

```bash
agno-harness init . --channel cli
```

Skills live next to the agent that uses them: `app/agents/<name>/skills/<skill>/SKILL.md`. A skill's `cards:` list must name schemas already registered on that agent's `CARD_CATALOG`.

To add a specialist, add `app/agents/<name>/` with `agent.py`, `instructions.md`, and `tools/`, then pass `build()` into `assemble()` on the coordinator. A specialist with no tools of its own can stay `app/agents/<name>.py`. Specialists do not get a channel. A Teams process uses `build_teams_resolver()`. Other channels use `build_user_resolver()`. The full first-project map is in the [README](../../../README.md).
