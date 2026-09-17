# agno-relay 文档导航 (Index)

中文正文。协议名、类型、文件名保持英文。

---

## 快速查阅矩阵：何时读哪篇？

| 你的工作角色 / 任务目标 | 核心推荐文档 | 重点章节与关键概念 |
| :--- | :--- | :--- |
| **快速上手与后端接入** | [getting-started.md](getting-started.md)<br>[agents.md](agents.md) | `OpenAILike` 铁律、`telemetry=False`、最小服务搭建、Tool 过滤器 |
| **理解架构与分层选型** | [architecture.md](architecture.md) | `RelayApp` (全渠道网关) vs `AguiRuntime` (执行内核) 深度对比与协同 |
| **UI 卡片与数据事实注入** | [cards.md](cards.md) | **Class-First** 自包含卡片、`resolver` 事实补全、N合1 卡片批量聚合 |
| **会话拓扑与长对话治理** | [sessions.md](sessions.md)<br>[compression.md](compression.md) | Ivy 式 `thread_key:sender` 隔离、**25h 空闲超时 (25h Idle TTL)**、高密度检查点压缩（100% KV 命中） |
| **多渠道适配 (Web/CLI/IM)** | [channels.md](channels.md) | `stream_mode="final"` 免疫 429、飞书免公网长连接、Teams M365 SDK |
| **完整 Agent Web 前端开发** | [frontend-ui.md](frontend-ui.md) | Thread 展示最佳实践、`order[]` 渲染合同、`/attach` 断网重连与 0s Ping |
| **人机协同审批 (HITL)** | [hitl.md](hitl.md) | 暂停等待人工输入、4 种交互审批场景、无感恢复执行流 |
| **动态任务规划 (Todo)** | [todo-workflow.md](todo-workflow.md) | 动态规划、状态流转（pending → in_progress → completed）与卡片联动 |
| **多智能体协作与委托** | [multi-agent.md](multi-agent.md) | 子 Agent 进度流透传 (`substream`)、父子上下文隔离 |
| **生产审计与存储持久化** | [persistence.md](persistence.md) | Ingestion Sinks、SQLite / PostgreSQL 归档、Redis 跨进程 Follow |
| **全链路可观测性 (OTel)** | [observability.md](observability.md) | OpenTelemetry 与 Langfuse 链路追踪，自动修复异步 Span 遗漏 |
| **底座开发与测试验收** | [testing.md](testing.md) | 金标回放测试 (Golden Traces)、时序状态机 (`Sequencer`)、`make check` 基线 |

---

## 规范与守则

- [SPEC.md](../SPEC.md) — 底层协议规约全览（Two Cardinal Rules、Wire Protocol、Ivy Topology）。
- [AGENTS.md](../AGENTS.md) — 面向智能体/开发者的架构红线与编码防呆守则（严禁全局装饰器、零循环导入、分层隔离）。
