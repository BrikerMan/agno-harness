# agno-harness

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)

> **Enterprise agent scaffolding. You write the business and the cards. The rest is production-ready.**

[中文文档](README_zh.md) | [Docs](docs/en/README.md) | [SPEC.md](SPEC.md) | [AGENTS.md](AGENTS.md)

---

Writing an Agno agent is fast. Shipping one people can refresh, leave running for half an hour, and drop into a Lark group means rebuilding protocol, compression, resume, cards, and rate limits. agno-harness turns that into assembly: Agno Runtime + Relay. Your product owns the business logic and the cards. Compression, long-runs, and channels are already there — and swappable.

---



## What actually sets it apart

**Close the tab. The run keeps going.** Closing a tab just stops watching. The agent keeps working. Reopen and it continues mid-sentence — cards, reasoning, child panels included. History is what the user saw, not the lossy model session. A run dies only when you abort it.

**Long sessions shrink. The KV cache stays hot.** Stock Agno compression fires a sync LLM call per tool, stalls the UI 10–20s, and rewrites the prefix — prompt cache is gone. We compress once, only when tokens actually cross the limit, by **appending** a checkpoint at the end of the transcript. That request keeps the previous prefix intact: **100% KV-cache hit**. Later turns keep eating the prefix cache. One pass over intent, facts, and next steps — not a summary per tool. Dangling tool calls get sealed so the next turn is not a 400.

**The business and the cards are yours.** Hang work on a Module / Toolkit. A card is one class: schema, `resolve()` for facts, `render_`* per channel. The model emits IDs; the server fills posters, ratings, deep links. Todos replace in place. Long docs and decks stream in sections — thousands of HTML lines never hit the SSE.

**One agent, in the browser and in the group.** The web app can stream word by word. In Lark or Teams it does not type into the channel — it marks “working”, then sends one finished card when it is done. Ten search hits become one notification, so the group is not flooded. A DM keeps context; in a group each person has their own thread of memory. If the platform retries the same webhook, the same turn does not run three times. Local Lark needs no public URL.

**It stops when a human must decide, and you can see the helpers.** Need a confirmation, an extra sentence, or an approval? The agent waits, then continues after the click. A helper sent off to research works in its own area — thinking and cards included. The main agent only takes the conclusion, so memories do not get mixed.

---



## How you assemble it


|             | What you get                                        | Entry                                       |
| ----------- | --------------------------------------------------- | ------------------------------------------- |
| **Runtime** | Resume, compression, cards, wait-for-human, helpers | `AgentRuntime` + `make_agui_router`          |
| **Relay**   | The same agent on Lark / Teams / CLI                | `RelayApp` + `LarkChannel` / `TeamsChannel` |


Web or desktop: Runtime. Group chat as well: add Relay. Agent code stays one.

```bash
pip install "agno-harness[fastapi,sqlite]"   # Runtime
pip install "agno-harness[teams,lark]"       # add Relay
pip install "agno-harness[all]"
```

```python
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai.like import OpenAILike
from fastapi import FastAPI

from agno_harness import AgentRuntime, SmartCompressionManager, make_agui_router
from agno_harness.runtime.longrun import LongRunManager

db = SqliteDb(db_file="sessions.db")
model = OpenAILike(id="...", api_key=..., base_url=...)
agent = Agent(
    model=model,
    db=db,
    compression_manager=SmartCompressionManager(model=model),
    add_history_to_context=True,
    num_history_runs=100,
    telemetry=False,
)
runtime = AgentRuntime(agent=agent, db=db)
app = FastAPI()
app.include_router(make_agui_router(runtime, long_runs=LongRunManager(runtime)))
```

`POST /agui?long-run=1` streams SSE. Reconnect on `GET /runs/{id}/attach`. History is `/frames`.

```python
from agno_harness import RelayApp, CLIChannel, LarkChannel

app = RelayApp(agent)
app.add_channel(CLIChannel())
app.add_channel(LarkChannel(app_id="...", app_secret="...", use_websocket=True))
app.serve()
```

Mount into an existing FastAPI app with `relay.get_router(resolve_user_id=...)`.

```bash
make run    # examples/01_cli_demo.py
```

---



## Where to read next

Index: [docs/en/README.md](docs/en/README.md) · [docs/zh/README.md](docs/zh/README.md)


| You are…                    | Start here                                                                                                                  |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Compression / todos / cards | [Cookbook](docs/en/00-agent-cookbook/README.md) → [compression](docs/en/02-interactions/04-compression-and-sealing/README.md) · [todos](docs/en/02-interactions/03-todo/README.md) · [cards](docs/en/02-interactions/01-class-first-cards.md) |
| Web refresh-and-resume      | [Web React](docs/en/03-clients/01-web-react/README.md) · [attach](docs/en/03-clients/01-web-react/04-attach-and-longrun.md) |
| Lark / Teams                | [Lark](docs/en/00-agent-cookbook/04-lark-feishu-agent.md) · [Teams](docs/en/00-agent-cookbook/03-teams-bot-agent.md)        |
| HITL / multi-agent          | [HITL](docs/en/02-interactions/02-hitl-and-actions/README.md) · [delegation](docs/en/02-interactions/06-multi-agent-delegation.md) |


---



## License

MIT © [Eliyar Eziz](https://github.com/eliyar-eziz)