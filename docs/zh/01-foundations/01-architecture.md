# 01-foundations / 系统架构与分层全景 (Architecture Overview)

`agno-harness` 是企业级 Agent 脚手架：**Agno Runtime + Relay**。Runtime 负责 Agent 怎么跑、UI 怎么还原；Relay 负责同一套代码进飞书 / Teams / CLI。业务和卡片你自己写。

---

## 1. 架构总览

整个系统遵循严格的分层防线（Strict Architectural Layering），杜绝循环导入与边界渗透：

```
+-----------------------------------------------------------------------------------+
|                            企业应用接入层 (Enterprise Host)                         |
|   Existing FastAPI App (app.include_router(relay.get_router()))  /  RelayServer   |
+-----------------------------------------------------------------------------------+
                                         │
+-----------------------------------------------------------------------------------+
|                        RelayApp 全渠道应用网关 (Multi-Channel App)                 |
|  - Webhook Idempotency (DeduplicationCache)                                       |
|  - Reaction Lifecycle Safety (try-finally settle ✅/❌)                           |
|  - Interactive Action Dispatcher (@relay.action)                                  |
|  - Session Lifecycle & Topology (SessionManager + User-defined SessionKeyResolver)|
+-----------------------------------------------------------------------------------+
         │                                                            │
         ▼                                                            ▼
+---------------------------------------+           +-------------------------------+
|         Transport Channels            |           |         Message Sinks         |
|  - Web (SSE / Wire Protocol)          |           |  - InMemorySink               |
|  - CLI (Rich Console)                 |           |  - SQLiteSink                 |
|  - Teams (Bot Framework / Adaptive)   |           |  - PostgresSink               |
|  - Lark (Event WebSocket / Cards v2)  |           |                               |
+---------------------------------------+           +-------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
|                       AgentRuntime（执行内核）                                      |
|  - EventTranslator & Sequencer (时序修复状态机)                                    |
|  - SmartCompressionManager & In-Context Checkpointing                             |
|  - Clean Seal Engine (未闭合工具调用补齐与密封)                                       |
|  - First-Class Observability (Langfuse / OpenInference 多维标签注入)               |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
|                             AGNO Agent 执行引擎                                    |
|                   agno.agent.Agent.arun() + Model Providers                       |
+-----------------------------------------------------------------------------------+
```

---



## 2. 核心命名体系与职责边界

在 `agno-harness` 中，核心组件职责清晰明确：


| 命名组件                                  | 职责定义                                                                          | 典型调用场景                                                  |
| ------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------- |
| `RelayApp`                            | 全渠道网关总控。消费 `AgentRuntime` 的 AG-UI 流，编排 Channel、SessionManager、Sinks，提供 Webhook 幂等去重、Reaction ACK 与卡片动作路由。 | `relay = RelayApp(runtime=runtime)`                     |
| `make_relay_router`                   | **推荐的 FastAPI 挂载函数**。将全功能 AG-UI 协议与 Channel Webhook 打包为标准 `APIRouter`。        | `app.include_router(relay.get_router(...))`             |
| `RelayServer`                         | **开箱即用的类封装 FastAPI 服务器**。支持 OOP 继承定制，适合独立运行的网关微服务。                            | `class MyServer(RelayServer): ...`                      |
| `AgentRuntime`                        | 纯粹的 Agent 执行内核。驱动 Agno Agent/Team，将思考、工具与卡片编译为标准 AG-UI 协议事件流。 | `runtime = AgentRuntime(agent=my_agent)`                |
| `CardCatalog`                         | *卡片元数据与跨渠道渲染编排器。负责管理 Class-First 卡片组件。*                                       | `catalog = CardCatalog([MovieCard])`                    |
| `ConversationKey`                     | 统一描述各端（Web/CLI/Teams/Lark）会话拓扑的数据对象。                                          | `key = ConversationKey(platform="teams", ...)`          |
| `SessionManager`                      | 会话持久化与 25 小时空闲超时治理，支持用户自定义 `SessionKeyResolver`。                              | `sess_id, is_new = await sm.get_or_create_session(key)` |


---



## 3. 分层隔离铁律 (Layering Guardrails)

为了保证模块的高内聚与低耦合，`tests/test_layering.py` 自动化检测以下架构红线：

1. `core/` **零外部依赖**：
  - 只能导入标准库和 Pydantic。
  - **严禁**导入 `agno`、`fastapi`、渠道 SDK 或数据库驱动。
2. `runtime/` **专注执行**：
  - 可以导入 `agno` 执行推理，但**严禁**导入 `fastapi` 或 Web 相关库。
3. `channels/` **渠道正交隔离**：
  - 各渠道（Web, CLI, Teams, Lark）相互隔离，不得跨渠道直接调用。
  - 依赖的外部 SDK（如 `botbuilder-core`, `lark-oapi`）采用按需懒加载。
4. `sinks/` **审计解耦**：
  - 归档消息与主执行流程完全异步解耦，审计写入失败决不中断用户消息流。

---

## 4. Sequencer / Module / 测试分层

`EventSequencer` 在 runtime 里修 AG-UI 时序（缺 START 补上、乱序收口），生产默认 `SequencerMode.AUDIT`。线级事件与违规形状见 [第三层 02](../04-deep-dive-and-faq/02-protocol-wire-spec.md)。

`Module`（StreamUI、SubAgent、Observability、CustomEvents）只认自己的 `CUSTOM` 命名空间，互相不 import。新能力优先加 Module，不要在 translator 里堆分支。

分层红线由 `tests/test_layering.py` 扫 AST。怎么写 FakeAgent / Golden：[06 测试](06-testing-and-verification.md)。

---

## 5. 三种用法与一轮生命周期

上层可以不用下层。三种接法：

| 用法 | 你写什么 | 何时用 |
| --- | --- | --- |
| **托管** | `runtime.stream_events(run_input)` | Web / IM / 默认路径 |
| **半托管** | 再加上 pre-run / post-run hook，可变 `RunScope` | 起名、检查点、改 `user_id` |
| **全手动** | 自己 `agent.arun()`，chunk 喂给 `EventTranslator` | 已经有一套 Agno 循环，只想要 AG-UI 帧 |

一轮顺序：

```text
RUN_STARTED
  → pre-run hooks
  → 可选 STATE_SNAPSHOT
  → 每颗 Agno chunk：官方 HANDLERS → parsers（只追加）→ modules → sequencer
  → post-run hooks
  → RUN_FINISHED / RUN_ERROR
```

客户端断线则没有终态事件（`none` 下自己把 `isStreaming=false`）。Parser 怎么 yield `CUSTOM`：[07 写一个 Agent](07-writing-an-agent.md)。

`BridgeModule`：名字 + 可选 `CUSTOM` 命名空间。同名 / 同命名空间组装时冲突即失败。`CUSTOM` 对 sequencer 无意义，模块必须自己在 `on_run_finish` 关掉括号。`StreamUIModule` 管 `<stream-ui>`、逐行 `ui.item`、`emit_text`、以及 `artifact_root_dir` 模板与防路径穿越。

