# 架构设计与分层规范 (Architecture)

`agno-relay` 采用严格的**单向依赖分层架构**，实现 Agent 业务逻辑与底层通信渠道的完全解耦。

---

## 1. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                         RelayApp                             │  <- 统一网关中枢
├──────────────┬──────────────┬────────────────┬───────────────┤
│ WebChannel   │ CLIChannel   │ LarkChannel    │ TeamsChannel  │  <- Transport 渠道层
├──────────────┴──────────────┴────────────────┴───────────────┤
│           Stream Controller & MessageCollector               │  <- 流控与卡片聚合层
│  (StreamMode: FINAL / THROTTLE / RAW; N items -> 1 Card)     │
├──────────────────────────────────────────────────────────────┤
│               SessionManager & ConversationKey               │  <- 会话拓扑层
│     (Ivy-style 拓扑隔离: thread_key:sender; 25h Idle TTL)     │
├──────────────────────────────────────────────────────────────┤
│               AguiRuntime (执行与编排内核)                   │  <- 运行时核心
│  (Sequencer 乱序纠偏, StreamUI 解析, Checkpointing, Seal)    │
├──────────────────────────────────────────────────────────────┤
│               CardCatalog (Class-First 自包含卡片)           │  <- 视图与渲染标准
├──────────────────────────────────────────────────────────────┤
│               Ingestion Sinks (SQLite / Postgres)            │  <- 审计归档层
└──────────────────────────────────────────────────────────────┘
```

---

## 2. 核心模块分层职责

### 2.1 Core (`agno_relay.core`)
纯协议定义与抽象基类，**禁止导入任何框架（FastAPI、Agno、渠道 SDK）**：
- `protocol.py`: AG-UI 协议版本与常量规约；
- `channel.py`: `Channel` 异步协议、`ConversationKey`、`ChannelEvent`、`OutboundMessage`；
- `streamui/`: 声明式卡片 Schema 基类 (`BlockSchema`, `ItemSchema`, `CardSchema`) 与 `CardCatalog`；
- `sequencer.py`: 严格的时序状态机，防止乱序与格式穿透。

### 2.2 Runtime (`agno_relay.runtime`)
AGNO 执行驱动引擎，**禁止导入 Web 框架（FastAPI）**：
- `AguiRuntime`: Agent 执行中枢，调度流式分块翻译；
- `compression.py`: 上下文高密度检查点压缩；
- `closure.py`: Clean Seal 干净封口与 XML 文本剥离；
- `longrun.py`: 后台长任务与会话恢复引擎。

### 2.3 Sessions (`agno_relay.sessions`)
会话拓扑与生命周期：
- `ConversationKey`: 自动根据单聊、群聊、频道子线程生成隔离 key；
- `SessionManager`: 维护 `25h Idle TTL`，超时自动安全切换全新会话，防止上下文暴涨。

### 2.4 Stream (`agno_relay.stream`)
削峰流控与卡片聚合：
- `MessageCollector`: 拦截流式事件，在 `ui.block.end` 时将内部所有碎片 item 聚合为平台单一原生大卡片；
- `ThrottledStreamBuffer`: 1.5s 窗口节流缓冲器，解决 429 报错；
- `StreamMode`: `RAW` (Web/CLI)、`FINAL` (IM平台默认)、`THROTTLE`。

### 2.5 Channels (`agno_relay.channels`)
外设接入适配器：
- `WebChannel`: 基于 FastAPI 的 AG-UI SSE 路由；
- `CLIChannel`: 基于 Rich 的本地彩色控制台交互通道；
- `LarkChannel`: 飞书 WebSocket 免公网 IP 长连接客户端；
- `TeamsChannel`: 微软 Teams M365 Agents SDK 适配器。

### 2.6 Sinks (`agno_relay.sinks`)
消息与会话异步审计通道：
- `SQLiteSink`: 本地轻量级 sqlite 归档；
- `InMemorySink`: 测试与调试桩。
