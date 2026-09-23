# agno-harness

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)

> **Your agent runs locally. You don't know where to start when you need to give it to customers.**

[中文文档](README_zh.md) | [Docs](docs/en/README.md) | [SPEC.md](SPEC.md) | [AGENTS.md](AGENTS.md)

It chats fine in the terminal. The moment you want colleagues, customers, or a group to use it, you find the prompt is not what's missing: who is using it, where the chat left off, whether two people mix up, how to look up a mistake, how to put it on the web and in a group.

agno-harness covers that layer. You still write what the agent should do. The same agent runs in the terminal, the browser, Teams, and Lark. Chat history, documents, skills, tools, tracing, login, and conversations are already built in.

If someone refreshes, it keeps answering. If they come back tomorrow, it still remembers. Two people in a group do not mix chats. If it says something wrong, you can open that turn. If it needs a person, it waits.


|             | What you get                                                                                          | Entry                        |
| ----------- | ----------------------------------------------------------------------------------------------------- | ---------------------------- |
| **Runtime** | Remembers the chat, notes, skills, tools, traces, resume, compression, cards, waiting for a person, helpers | `AgentRuntime`               |
| **Relay**   | Who is talking, which conversation. Same agent in the terminal, the browser, Teams, and Lark         | `RelayApp` + `app/channels/` |


One process. One Runtime. Only the main agent sits on the Runtime. Helpers are specialists it can call. Their work shows in a side panel. The notes it searches live in `data/knowledge/`.

---

## If you have already shipped an agent

The details that usually get rewritten per product:

**Close the tab. The run keeps going.** Closing a tab just stops watching. The agent keeps working. Reopen and it continues mid-sentence — cards, reasoning, child panels included. History is what the user saw. A run dies only when you abort it. [Long-run / attach](docs/en/03-clients/01-web-react/04-attach-and-longrun.md)

**Long sessions shrink. The KV cache stays hot.** Compression runs once, only when tokens actually cross the limit, by **appending** a checkpoint at the end of the transcript. That request keeps the previous prefix intact. Later turns keep using that prefix. Dangling tool calls get sealed so the next turn stays valid. [Compression](docs/en/02-interactions/04-compression-and-sealing/README.md)

**The cards are yours.** One class: schema, `resolve()` for facts, `render_`* per channel. The model emits IDs; the server fills posters, ratings, deep links. [Cards](docs/en/02-interactions/01-class-first-cards.md)

**One agent, in the browser and in the group.** The web app can stream word by word. In Lark or Teams it marks “working”, then sends one finished card. If the platform retries the same webhook, the same turn does not run again. Local Lark needs no public URL.

**It stops when a human must decide.** Confirmation, an extra sentence, an approval — the agent waits, then continues after the click. [HITL](docs/en/02-interactions/02-hitl-and-actions/README.md)

---

## Create an agent

Same steps for a person and for a coding agent. Python 3.11 or newer.

```bash
python3 --version
pip install "agno-harness[fastapi,teams,lark]"
```

With [uv](https://docs.astral.sh/uv/): `uv add "agno-harness[fastapi,teams,lark]"`.

Start in the terminal. No bot account and no public URL.

```bash
mkdir my-agent && cd my-agent
agno-harness init . --channel cli
cp .env.example .env
```

Fill these three lines in `.env`. Any OpenAI-compatible gateway works (OpenAI, OpenRouter, DashScope, or a local server).

```env
AGNO_HARNESS_LLM_BASE_URL=https://your-gateway.example/v1
AGNO_HARNESS_LLM_API_KEY=sk-your-key
AGNO_HARNESS_LLM_MODEL=gpt-4o
```

```bash
python agent.py
```

Type a message. `/reset` starts a fresh chat. `/exit` quits. The key stays in `.env`. `.gitignore` already ignores that file.

`--channel` chooses the process entry. Every project gets the same tree: a coordinator, one sample specialist (`AgentBuilder`), Markdown notes, one skill, tools, cards, and a `channels/` package. `AgentBuilder` does not know your product. The resolvers in `app/identity.py` trust a header or a shared local user. Replace both before this process serves anyone but you.

```text
agent.py                         process entry
app/main.py                      FastAPI app (create_app)
app/cli.py                       terminal loop
app/identity.py                  build_user_resolver; Teams uses build_teams_resolver
app/paths.py                     data/agent.db, data/knowledge/
app/knowledge/                   seed notes, copied into data/knowledge/ when missing
data/                            gitignored databases and the notes the agent searches
app/agents/main/                 coordinator
  instructions.md                voice and when to delegate
  tools/                         one Toolkit per file, listed in TOOLS
  cards/                         one card class per file, registered on CARD_CATALOG
  actions/                       button handlers
  skills/<name>/SKILL.md         loaded on demand
app/agents/researcher/           web research specialist
  instructions.md                how search results are reported
  tools/exa.py                   public Exa search, no API key
app/agents/agent_builder.py      one-file sample specialist
app/channels/                    one module per transport
  __init__.py                    mount_all calls mount_teams(relay, app) before mount_web
app/services/                    shared functions
```


| Change this                     | Edit                                                                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| How it talks, when it delegates | `app/agents/main/instructions.md`                                                                                                         |
| A fact it should look up        | `data/knowledge/*.md`. Seeds live in `app/knowledge/` and copy across only when the file is missing.                                      |
| Who is calling                  | `app/identity.py`. Teams mounts pass `build_teams_resolver()`.                                                                            |
| A new tool                      | `app/agents/<name>/tools/<tool>.py`, then append it to `TOOLS`                                                                            |
| A new card                      | a `BlockSchema` in `cards/`, register it on `CARD_CATALOG`                                                                                |
| A new skill                     | `skills/<name>/SKILL.md`. `cards:` names must already be on that agent's catalog                                                          |
| A new specialist                | `app/agents/<name>/` with `agent.py`, `instructions.md`, and `tools/`, then pass `build()` into `assemble()` in `app/agents/main/agent.py`. A specialist with no tools of its own can stay `app/agents/<name>.py` |
| A new channel                   | `app/channels/<name>.py` with `mount(relay, app)`, import it, and call it inside `mount_all` before `mount_web`                           |


Each module docstring in the generated project repeats the rule for that folder. Follow the file that is already there.

Helpers are not mounted on a channel. The coordinator is the only agent on `AgentRuntime`.

### Channels


| `--channel`     | What `python agent.py` does                                                                                                                                                |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cli`           | Calls `cli.mount` and reads the terminal. FastAPI stays in `app/main.py` and is not started.                                                                               |
| `web`           | Serves port 8000. Chat API is `POST /api/v1/channels/web/agui`. Pair it with [Web / React](docs/en/03-clients/01-web-react/README.md) or the [frontend kit](resources/frontend-kit/README.md). |
| `teams`         | `mount_all` calls `mount_web` and `mount_teams`. Azure messaging URL is your public host plus `/api/v1/channels/teams/messages`.                                                             |
| `lark`          | `mount_all` calls `mount_lark`. The bot connects out over a WebSocket.                                                                                                     |
| `all` (default) | `mount_all` calls web, teams, lark, and cli.                                                                                                                               |


An enabled channel with missing settings logs an error and the process stops. Fill the keys before you start that channel.

Teams:

```bash
agno-harness init . --channel teams
cp .env.example .env
agno-harness teams onboard
agno-harness teams doctor
python agent.py
```

Walkthrough: [03 Teams](docs/en/00-agent-cookbook/03-teams-bot-agent.md).

Lark:

```bash
agno-harness init . --channel lark
cp .env.example .env
agno-harness lark onboard
python agent.py
```

Walkthrough: [04 Lark](docs/en/00-agent-cookbook/04-lark-feishu-agent.md).

`POST /api/v1/channels/web/agui?long-run=1` streams SSE. Reconnect on `GET /api/v1/runs/{id}/attach`. History is `GET /api/v1/threads/{id}/frames`. Health is `GET /api/v1/health`. An existing FastAPI app can mount `relay.get_router(resolve_user_id=...)`.

---

## Where to read next

Index: [docs/en/README.md](docs/en/README.md) · [docs/zh/README.md](docs/zh/README.md)


| You are…                    | Start here                                                                                                                                                                                                                                    |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Notes, skills, helpers      | [07 Markdown notes](docs/en/00-agent-cookbook/07-teams-knowledge-bot.md) · [delegation](docs/en/02-interactions/06-multi-agent-delegation.md) · [skills](docs/en/02-interactions/07-skills-and-jit.md)                                        |
| Compression / todos / cards | [Cookbook](docs/en/00-agent-cookbook/README.md) → [compression](docs/en/02-interactions/04-compression-and-sealing/README.md) · [todos](docs/en/02-interactions/03-todo/README.md) · [cards](docs/en/02-interactions/01-class-first-cards.md) |
| Web refresh-and-resume      | [Web React](docs/en/03-clients/01-web-react/README.md) · [frontend-kit](resources/frontend-kit/README.md) · [attach](docs/en/03-clients/01-web-react/04-attach-and-longrun.md)                                                                |
| Lark / Teams                | [Lark](docs/en/00-agent-cookbook/04-lark-feishu-agent.md) · [Teams](docs/en/00-agent-cookbook/03-teams-bot-agent.md)                                                                                                                          |
| HITL                        | [HITL](docs/en/02-interactions/02-hitl-and-actions/README.md)                                                                                                                                                                                 |


---

## License

MIT © [Eliyar Eziz](https://github.com/eliyar-eziz)