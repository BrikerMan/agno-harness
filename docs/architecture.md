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

---

## 3. RelayApp 与 AguiRuntime 的关系与选型

在实际工程落地中，开发者最容易困惑的问题是：**`RelayApp` 和 `AguiRuntime` 究竟是什么关系？我该用哪一个？**

### 3.1 核心对比表

| 维度 | `AguiRuntime` (执行内核) | `RelayApp` (全渠道底座网关) |
| :--- | :--- | :--- |
| **定位** | **Agent 执行内核与协议转换器** | **全渠道生产网关机盘 (Chassis)** |
| **关注点** | 文本 Token 流、XML 围栏解析、时序纠错、上下文压缩 | 外部渠道适配、网络防 429、群聊会话隔离、卡片 N 合 1 聚合、消息审计 |
| **输入输出** | 输入标准 `RunAgentInput`，输出纯 `BaseEvent` 原始事件流 | 输入 `ChannelEvent`（来自飞书/Teams/CLI/Web），输出 `OutboundMessage`（包含整包卡片） |
| **会话模型** | 无会话拓扑概念，只认 `thread_id` 和 `run_id` | 拥有 `ConversationKey`，自动按 `thread_key:sender` 隔离，内置 **25 小时空闲超时（25h TTL）** |
| **流控策略** | 不感知下游客服端或 IM 的限流机制 | 具备 `StreamMode.FINAL`、`THROTTLE`、`RAW`，免疫 Teams/飞书 429 报错 |
| **适用场景** | 仅开发独立的 Web AG-UI 后端，或自定义执行流 | 企业生产环境：机器人（飞书/Teams）、多渠道接入、Kino-Work 复杂编排底座 |

### 3.2 协同模式

`RelayApp` 在底层**直接包裹并驱动** `AguiRuntime`。你可以采用两种使用方式：

#### 模式 A：开箱即用（传入 Agent，RelayApp 自动包裹）
```python
from agno.agent import Agent
from agno_relay import RelayApp, CLIChannel, LarkChannel

agent = Agent(name="Assistant", instructions="你是一个助手。")

# RelayApp 内部自动构造 AguiRuntime
app = RelayApp(agent)
app.add_channel(CLIChannel())
app.add_channel(LarkChannel(app_id="...", app_secret="..."))
app.serve()
```

#### 模式 B：深度定制（显式组装 AguiRuntime 后注入 RelayApp）
当你需要自定义 Store、HITL 审批、OTel 追踪或特定模块时，可以手动构建 `AguiRuntime`，再挂载到 `RelayApp`：
```python
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno_relay import AguiRuntime, RelayApp, CLIChannel

db = SqliteDb(db_file="sessions.db")
agent = Agent(name="Assistant", db=db)

# 1. 深度定制的执行内核
runtime = AguiRuntime(
    agent=agent,
    db=db,
    enable_reasoning_patch=True,
    sequencer_mode=SequencerMode.REPAIR,
)

# 2. 注入 RelayApp 获得全渠道治理能力
app = RelayApp(runtime)
app.add_channel(CLIChannel())
app.serve()
```
