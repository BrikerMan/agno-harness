# agno-relay 文档导航 (Index)

欢迎使用 `agno-relay`。本文档库帮助你深入理解和使用企业级多渠道 Agent 网关底座：

- **架构与分层总览**：[`docs/architecture.md`](architecture.md) — 了解 `RelayApp` 与 `AguiRuntime` 的分层关系、单向依赖设计与核心数据流。
- **Class-First 卡片组件**：[`docs/cards.md`](cards.md) — 掌握自包含卡片写法、“模型选主键，服务端补事实”与 N 合 1 卡片聚合逻辑。
- **会话拓扑与生命周期**：[`docs/sessions.md`](sessions.md) — 理解 `ConversationKey` 多人隔离模型、25 小时空闲超时（25h Idle TTL）与指令重置。
- **多渠道接入与流控机制**：[`docs/channels.md`](channels.md) — 搞懂 Web、CLI、飞书免公网长连接、Teams 以及 `stream_mode` 防 429 秘诀。
- **协议规范详情**：[`SPEC.md`](../SPEC.md) — 底层协议规约全览。
- **AI 编码规范守则**：[`AGENTS.md`](../AGENTS.md) — 面向智能体/开发者的架构红线与开发基线。
