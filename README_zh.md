# agno-harness (中文指南)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> **本地 Agent 能跑。一要发给客户，就不知道从哪下手。**

[English Documentation](README.md) | [完整文档](docs/zh/README.md) | [更新日志](CHANGELOG.md) | [协议规范 (SPEC.md)](SPEC.md) | [开发守则 (AGENTS.md)](AGENTS.md)

终端里聊得挺好。真要给同事、给客户、丢进飞书群，才发现缺的不是 prompt：谁在用、聊到哪了、两个人会不会串、出了错怎么翻、网页和群怎么接。

agno-harness 把这一层接好。你还是写它该干什么。同一套进终端、浏览器、飞书、Teams — 历史、知识、技能、工具托管、追踪、鉴权、会话，底座已经有了。

刷新了，它还在答。第二天来，它还认识。两个人在群里 @ 它，不会串台。它说了句怪话，你能翻出那一轮。该问人的时候会停。

| | 你拿到的 | 入口 |
| --- | --- | --- |
| **Runtime** | 记得住对话、笔记、技能、工具、追踪、续写、压缩、卡片、等人确认、帮手 | `AgentRuntime` |
| **Relay** | 谁在说话、哪段对话，同一套 Agent 进终端、浏览器、Teams、飞书 | `RelayApp` + `app/channels/` |

一个进程，一个 Runtime。Runtime 上只挂主 Agent（coordinator）。帮手是主 Agent 通过 `SubAgentToolkit` 叫来的专家：工作过程出现在子面板里。它搜索的笔记在 `data/knowledge/`。

---

## 如果你已经上线过 Agent

每个产品通常会重写一遍的那些细节：

**刷新了，任务还在跑。** 关 tab 只是不看了，后台接着干。再打开从半句续上，卡片、思考、子面板原样回来。历史回放的是当时用户看见的画面。只有你显式中止，任务才会停。[长任务 / attach](docs/zh/03-clients/01-web-react/04-attach-and-longrun.md)

**长对话压下去，KV Cache 还在。** 只在 Token 真超限时压一次，检查点**追加在消息流末尾**，生成当轮的前缀不变。后面几轮继续吃这份前缀。中止或缺结果的 tool call 会被封口，下一轮仍然合法。[压缩](docs/zh/02-interactions/04-compression-and-sealing/README.md)

**卡片是你的。** 一个 class：schema、`resolve()` 补事实、各端 `render_*`。模型只吐 ID，服务端补海报、评分、深链。[卡片](docs/zh/02-interactions/01-class-first-cards.md)

**同一套 Agent，进浏览器也进群。** 网页上可以一个字一个字往外蹦。丢进飞书或 Teams，先标「正在处理」，跑完把结果收成一张卡。平台把同一条消息重推过来，同一轮不会再跑一遍。本地接飞书不用公网地址。

**该问人的时候会停。** 要确认、补一句、走个审批，Agent 会等，人点完接着干。[HITL](docs/zh/02-interactions/02-hitl-and-actions/README.md)

---

## 创建一个 Agent

人和写代码的 agent 走同一步。需要 Python 3.11 或更新。

```bash
python3 --version
pip install "agno-harness[fastapi,teams,lark]"
```

用 [uv](https://docs.astral.sh/uv/) 时：`uv add "agno-harness[fastapi,teams,lark]"`。

先从终端开始。不用申请机器人，也不用公网地址。

```bash
mkdir my-agent && cd my-agent
agno-harness init . --channel cli
cp .env.example .env
```

`.env` 里先填这三行。兼容 OpenAI 接口的服务都可以：OpenAI、OpenRouter、通义（DashScope），或你自己的本地服务。

```env
AGNO_HARNESS_LLM_BASE_URL=https://your-gateway.example/v1
AGNO_HARNESS_LLM_API_KEY=sk-your-key
AGNO_HARNESS_LLM_MODEL=gpt-4o
```

```bash
python agent.py
```

输入一句话。`/reset` 开一段新对话。`/exit` 退出。密钥只放在 `.env`。`.gitignore` 已经忽略这个文件。

`--channel` 决定进程怎么启动。每次 `init` 都是同一棵树：一个主 Agent、一个示例专家（`AgentBuilder`）、Markdown 笔记、一个技能、工具、卡片，以及 `channels/`。`AgentBuilder` 不了解你的产品。`app/identity.py` 里的解析信任请求头，或者把所有人当成同一个本地用户。对外服务之前，把这两处换成你自己的实现。

```text
agent.py                         进程入口
app/main.py                      FastAPI（create_app）
app/cli.py                       终端循环
app/identity.py                  默认 build_user_resolver；Teams 用 build_teams_resolver
app/paths.py                     data/agent.db、data/knowledge/
app/knowledge/                   笔记种子，缺文件时复制到 data/knowledge/
data/                            已 gitignore。数据库和 Agent 实际搜索的笔记
app/agents/main/                 主 Agent
  instructions.md                口吻，以及何时委托
  tools/                         一个 Toolkit 一个文件，登记到 TOOLS
  cards/                         一张卡一个类，登记到 CARD_CATALOG
  actions/                       按钮处理
  skills/<name>/SKILL.md         用到时再加载
app/agents/researcher/           网页检索专家
  instructions.md                检索结果怎么回报
  tools/exa.py                   公共 Exa 搜索，不需要 API key
app/agents/agent_builder.py      单文件示例专家
app/channels/                    一种传输一个模块
  __init__.py                    mount_all 先挂 Teams，再挂 mount_web
app/services/                    共用的业务函数
```

| 要改什么 | 改哪里 |
| --- | --- |
| 怎么说话、何时叫帮手 | `app/agents/main/instructions.md` |
| 它该记住的事实 | `data/knowledge/*.md`。种子在 `app/knowledge/`，只在目标文件还不存在时复制 |
| 谁在调用 | `app/identity.py`。Teams 进程传入 `build_teams_resolver()` |
| 新工具 | `app/agents/<name>/tools/<tool>.py`，再追加到 `TOOLS` |
| 新卡片 | `cards/` 里一个 `BlockSchema`，注册到 `CARD_CATALOG` |
| 新技能 | `skills/<name>/SKILL.md`。`cards:` 里的名字必须已经在这个 Agent 的 catalog 里 |
| 新专家 | `app/agents/<name>/`，里面是 `agent.py`、`instructions.md` 和 `tools/`，再在 `app/agents/main/agent.py` 里把 `build()` 传给 `assemble()`。没有自己工具的专家可以仍是 `app/agents/<name>.py` |
| 新渠道 | `app/channels/<name>.py` 写出 `mount(relay, app)`，在 `mount_all` 里、`mount_web` 之前调用 |

生成项目里每个模块的 docstring 写着该目录的扩展方式。照着已有文件加。

帮手不挂渠道。`AgentRuntime` 上只有主 Agent。默认的 `AgentBuilder` 是占位，上线前换掉。

### 渠道

| `--channel` | `python agent.py` 做什么 |
| --- | --- |
| `cli` | 直接调用 `cli.mount`，在终端对话。`app/main.py` 里的 FastAPI 不会启动。 |
| `web` | 监听 8000。对话接口是 `POST /api/v1/channels/web/agui`。网页用 [Web / React](docs/zh/03-clients/01-web-react/README.md) 或 [frontend kit](resources/frontend-kit/README_zh.md)。 |
| `teams` | `mount_all` 调用 `mount_web` 和 `mount_teams`。Azure 消息地址是公网主机加上 `/api/v1/channels/teams/messages`。 |
| `lark` | `mount_all` 调用 `mount_lark`。机器人用 WebSocket 自己连出去。 |
| `all`（默认） | `mount_all` 调用 web、teams、lark、cli。 |

渠道一旦写进 `mount_all`，缺配置会打一行错误并停掉进程。先把密钥填上再启动。

Teams：

```bash
agno-harness init . --channel teams
cp .env.example .env
agno-harness teams onboard
agno-harness teams doctor
python agent.py
```

完整步骤：[03 Teams](docs/zh/00-agent-cookbook/03-teams-bot-agent.md)。

飞书：

```bash
agno-harness init . --channel lark
cp .env.example .env
agno-harness lark onboard
python agent.py
```

完整步骤：[04 飞书](docs/zh/00-agent-cookbook/04-lark-feishu-agent.md)。

`POST /api/v1/channels/web/agui?long-run=1` 出 SSE。刷新走 `GET /api/v1/runs/{id}/attach`。历史走 `GET /api/v1/threads/{id}/frames`。健康检查是 `GET /api/v1/health`。已有 FastAPI 时，用 `relay.get_router(resolve_user_id=...)` 挂进去。

---

## 往下读

完整目录：[docs/zh/README.md](docs/zh/README.md)

| 你在做 | 先读 |
| --- | --- |
| 笔记、技能、帮手 | [07 Markdown 笔记](docs/zh/00-agent-cookbook/07-teams-knowledge-bot.md) · [委托](docs/zh/02-interactions/06-multi-agent-delegation.md) · [技能](docs/zh/02-interactions/07-skills-and-jit.md) |
| 压缩 / Todo / 卡片 | [菜谱](docs/zh/00-agent-cookbook/README.md) → [压缩](docs/zh/02-interactions/04-compression-and-sealing/README.md) · [Todo](docs/zh/02-interactions/03-todo/README.md) · [卡片](docs/zh/02-interactions/01-class-first-cards.md) |
| Web 刷新续写 | [Web React](docs/zh/03-clients/01-web-react/README.md) · [frontend-kit](resources/frontend-kit/README_zh.md) · [attach](docs/zh/03-clients/01-web-react/04-attach-and-longrun.md) |
| 飞书 / Teams | [飞书](docs/zh/00-agent-cookbook/04-lark-feishu-agent.md) · [Teams](docs/zh/00-agent-cookbook/03-teams-bot-agent.md) |
| HITL | [HITL](docs/zh/02-interactions/02-hitl-and-actions/README.md) |

---

## 开源协议

MIT License © [Eliyar Eziz](https://github.com/eliyar-eziz)
