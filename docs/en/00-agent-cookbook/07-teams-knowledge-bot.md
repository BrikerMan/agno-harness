# 07. Markdown notes

`agno-harness init` always writes notes, one skill, and one placeholder helper, `AgentBuilder`. `--channel` only chooses how the process starts. `AgentBuilder` and the resolvers in `app/identity.py` are marked `MUST CHANGE BEFORE PRODUCTION`.

Seed notes are `app/knowledge/*.md`. Startup copies a seed into `data/knowledge/` only when that file is missing. The agent searches `data/knowledge/`. Databases are `data/agent.db` and `data/sessions.db`. `data/` is gitignored. Edit a file under `data/knowledge/` and the next search reads it.

```bash
agno-harness init . --channel cli
```

Skills live next to the agent that uses them: `app/agents/<name>/skills/<skill>/SKILL.md`. A skill's `cards:` list must name schemas already registered on that agent's `CARD_CATALOG`.

To add a helper, add `app/agents/<name>.py` with `build()` and pass that agent into `assemble()` on the coordinator. Helpers do not get a channel. A Teams process uses `build_teams_resolver()`. Other channels use `build_user_resolver()`. The full first-project map is in the [README](../../../README.md).
