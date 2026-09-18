# agno-harness (中文指南)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> **企业级 Agent 脚手架。你写业务和卡片，底座已经能上生产。**

[English Documentation](README.md) | [完整文档](docs/zh/README.md) | [协议规范 (SPEC.md)](SPEC.md) | [开发守则 (AGENTS.md)](AGENTS.md)

---

写一个 Agno Agent 很快。做成用户敢刷新、敢跑半小时、敢丢进飞书群的产品，难的是协议、压缩、续写、卡片、限流各写一遍。`agno-harness` 把这些收成装配：**Agno Runtime + Relay**。业务和卡片按自己的产品来，压缩、长任务、渠道可以换，不必从零搭。

---

## 真正拉开差距的地方

**刷新了，任务还在跑。** 关 tab 只是不看了，后台接着干。再打开从半句续上，卡片、思考、子面板原样回来。历史回放的是当时用户看见的画面，不是模型 session 里那份被压过的残渣。只有你显式 abort，任务才会停。

**长对话压下去，KV Cache 还在。** Agno 原生压缩每出一个 tool 就同步打一次 LLM，卡 10–20 秒，前缀被改写，Prompt Cache 直接打穿。我们只在 Token 真超限时压一次，检查点**追加在消息流末尾**：生成当轮前缀不变，**KV Cache 命中率 100%**，后面几轮继续吃缓存。一次提炼意图、事实、下一步，不是每个 tool 单独摘要。中止或缺结果的 tool call 会被封口，下一轮不再 400。

**业务和卡片是你的。** 业务挂 Module / Toolkit。卡片一个 class：schema、`resolve()` 补事实、各端 `render_*`。模型只吐 ID，服务端补海报、评分、深链——省 Token，也防幻觉链接。Todo 原地替换，长文 / 幻灯片按段流，不会把几千行灌进 SSE。

**同一套 Agent，进浏览器也进群。** 网页上可以一个字一个字往外蹦。丢进飞书或 Teams，不会在群里刷屏打字——先标一下「正在处理」，跑完把结果收成一张卡发出去，完事再标「好了」。十个搜索结果合成一条通知，群不会被刷爆。私聊记得住上下文；群里每个人各聊各的，不会串。平台超时重推同一条消息，也不会把同一轮活干三遍。本地接飞书不用公网 IP。

**该问人的时候会停，叫帮手的时候看得见。** 要用户确认、补一句、走个审批，Agent 会停下来等，人点完接着干。派出去做调研的帮手，思考和卡片开在自己那一块，主 Agent 只拿回结论，大家的记忆不会搅成一锅。

---

## 怎么装配

| | 你拿到的 | 入口 |
| --- | --- | --- |
| **Runtime** | 刷新续写、智能压缩、卡片、等人确认、帮手 | `AgentRuntime` + `make_agui_router` |
| **Relay** | 同一 Agent 挂到飞书 / Teams / CLI | `RelayApp` + `LarkChannel` / `TeamsChannel` |

只做 Web / 桌面：用 Runtime。还要进群：再加 Relay。Agent 正文不用拆两套。

```bash
pip install "agno-harness[fastapi,sqlite]"   # Runtime
pip install "agno-harness[teams,lark]"       # 加上 Relay
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

`POST /agui?long-run=1` 出 SSE，刷新走 `GET /runs/{id}/attach`，历史走 `/frames`。

```python
from agno_harness import RelayApp, CLIChannel, LarkChannel

app = RelayApp(agent)
app.add_channel(CLIChannel())
app.add_channel(LarkChannel(app_id="...", app_secret="...", use_websocket=True))
app.serve()
```

已有 FastAPI 用 `relay.get_router(resolve_user_id=...)` 挂进去。

```bash
make run    # examples/01_cli_demo.py
```

---

## 往下读

完整目录：[docs/zh/README.md](docs/zh/README.md)

| 你在做 | 先读 |
| --- | --- |
| 压缩 / Todo / 卡片 | [菜谱](docs/zh/00-agent-cookbook/README.md) → [压缩](docs/zh/02-interactions/04-compression-and-sealing/README.md) · [Todo](docs/zh/02-interactions/03-todo/README.md) · [卡片](docs/zh/02-interactions/01-class-first-cards.md) |
| Web 刷新续写 | [Web React](docs/zh/03-clients/01-web-react/README.md) · [frontend-kit](resources/frontend-kit/README_zh.md) · [attach](docs/zh/03-clients/01-web-react/04-attach-and-longrun.md) |
| 飞书 / Teams | [飞书](docs/zh/00-agent-cookbook/04-lark-feishu-agent.md) · [Teams](docs/zh/00-agent-cookbook/03-teams-bot-agent.md) |
| HITL / 多 Agent | [HITL](docs/zh/02-interactions/02-hitl-and-actions/README.md) · [委托](docs/zh/02-interactions/06-multi-agent-delegation.md)

---

## 开源协议

MIT License © [Eliyar Eziz](https://github.com/eliyar-eziz)
