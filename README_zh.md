# agno-relay (中文指南)

[![PyPI Version](https://img.shields.io/pypi/v/agno-relay.svg)](https://pypi.org/project/agno-relay/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> **AGNO Agent 的企业级全渠道生产网关**  
> 一套 Agent 业务代码，无缝挂载运行在 **Web (AG-UI)**、**微软 Teams**、**飞书/Lark** 以及 **本地 CLI**。

[English Documentation](README.md) | [协议设计规范 (SPEC.md)](SPEC.md) | [开发防呆指引 (AGENTS.md)](AGENTS.md)

---

## 解决的核心痛点

在 Python 中用 AGNO 写一个 Agent 很简单，但要让它在企业真实办公群（Teams、飞书、企业微信）和 Web 稳定落地，会遇到几个致命的工程暗礁：

1. **高频 Edit 刷出 429 封禁**：如果在 Teams / 飞书中试图模拟 Web 的打字机逐字流式输出，调用消息编辑 API 会在 3 秒内被平台直接限流禁言；
2. **卡片疯狂刷屏**：搜索工具生成了 10 条推荐结果，传统做法连发 10 条卡片消息，群聊被瞬间炸锅；
3. **多人 @Bot 上下文串台**：多个同事在同一个群里向 Bot 提问，记忆混淆交叉，甚至泄露隐私；
4. **长对话爆上下文**：多轮群聊对话积累数天，Token 暴涨、KV 缓存击穿，甚至由于超时断连产生悬空 Tool Call 导致下一轮发话直接报错 400。

`agno-relay` 将这些复杂且痛苦的工程治理封装在底层底座中，让你只专注于 Agent 自身的业务 Prompt 与工具编写。

---

## 核心技术护城河

- 🛡️ **双流式模式与 429 免疫**：
  - `stream_mode="final"`（IM 推荐默认）：收到消息秒回 Reaction（🤔）并保持后台 Typing 心跳，执行完毕一次性整包发出完整 Markdown + 结构化卡片，最后打勾（✅），彻底免疫 429；
  - `stream_mode="throttle"`：1.5s 自适应时间窗节流输出；
  - `stream_mode="raw"`：面向 Web AG-UI 的零延迟 SSE 实时流。
- 🧩 **Class-First 自包含卡片组件**：
  - 彻底淘汰装饰器注册，杜绝跨文件循环导入；
  - 坚持 **“模型选 ID，服务端补事实”**：模型只输出紧凑的 XML 围栏与主键 ID，服务端异步 resolve 真实数据并自动转为 Teams Adaptive Card 与飞书卡片。
- 📦 **N 合 1 卡片批量聚合**：
  - 一个 Block 内生成的多个 item 在群聊中自动聚合为 **1 张精美大卡片**，群内通知只响一声。
- ⏳ **Ivy 级 25 小时会话拓扑**：
  - 会话按 `thread_key:sender` 隔离，群内各聊各的互不串台；
  - 内置 **25 小时闲置超时（25h Idle TTL）**：自然跨越“昨天下午到今天上午”的连续工作节奏，过期自动开启全新干净会话。
- ⚡ **In-Context 检查点压缩**：
  - 在上下文末尾追加高密度 Checkpoint，100% 保持 KV Cache 前缀缓存，长对话成本下降 90%。
- 🔒 **Clean Seal 干净封口**：
  - 用户中止、超时或网络中断时自动补齐悬空的 Tool Call，杜绝下一轮发话产生 400 Bad Request。
- 🌐 **飞书 WebSocket 免公网长连接**：
  - 本地开发无需公网 IP，无需配置域名或隧道，本地一条命令即刻接管群聊。

---

## 快速上手

```bash
# 安装基础包 (Web + CLI)
pip install agno-relay

# 支持 Teams
pip install "agno-relay[teams]"

# 支持飞书/Lark
pip install "agno-relay[lark]"

# 全量功能安装
pip install "agno-relay[all]"
```

```python
from agno.agent import Agent
from agno_relay import RelayApp, CLIChannel, LarkChannel

# 1. 编写标准的 AGNO Agent
agent = Agent(name="Assistant", instructions="你是一个专业的企业助手。")

# 2. 挂载到 agno-relay 运行时
app = RelayApp(agent)
app.add_channel(CLIChannel())
app.add_channel(LarkChannel(app_id="...", app_secret="...", use_websocket=True))

if __name__ == "__main__":
    app.serve()
```

---

## 开源协议

MIT License © [Eliyar Eziz](https://github.com/eliyar-eziz)
