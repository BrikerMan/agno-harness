# 01-foundations / 全链路可观测性 (First-Class Observability)

在企业级生产环境中，**可观测性（Observability）是一等公民**。
Agent 在多渠道运行、调用多种工具、委托子 Agent 执行时，运维与开发者必须能够通过清晰的 Trace 快速定位：
1. 这个会话是哪个用户在哪种端（Teams / 飞书 / Web）发起的？
2. 哪个阶段耗时最长（首字时延 TTFT、工具调用时延、子智能体执行）？
3. 生产环境中哪些请求发生过协议修复或中断？

`agno-harness` 开箱即用地与 **OpenTelemetry** 及 **Langfuse / Phoenix**（OpenInference 语义规范）无缝对接。

---

## 1. 快速接入 Langfuse

只需在环境中配置标准环境变量，网关在启动时会自动识别并建立 OTLP 导出链路：

```bash
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."
export LANGFUSE_BASE_URL="https://cloud.langfuse.com" # 或私有部署地址
export OTEL_SERVICE_NAME="enterprise-agent"
export OTEL_ENVIRONMENT="production"
```

两套互不重复，不要两头各记一遍 prompt / 工具：

- **`setup_otlp()`** 装上 `openinference-instrumentation-agno`：模型、工具、token。
- **`ObservabilityModule`**：一次 AG-UI run 一根 span（默认 `<agent-name>.turn-run`），挂用户可见的 input/output、TTFT、帧数、客户端是否还在。不重记 prompt / 工具。

没配 collector 就是 no-op。这和本地 JSONL **不是一回事**：`AGNO_HARNESS_TRACE_DIR` 写出 chunk → 事件对照，给 parser / Golden 用；OTLP 给 Langfuse / Phoenix。调试协议看目录，看生产看板看 collector。

或者在代码中显式初始化：

```python
from agno_harness.observability import setup_otlp

setup_otlp(
    service_name="enterprise-agent",
    environment="production",
    langfuse_public_key="pk-lf-...",
    langfuse_secret_key="sk-lf-...",
    langfuse_base_url="https://cloud.langfuse.com",
)
```

---

## 2. 根 Span 名字：`<agent-name>.turn-run`

每一轮 AG-UI run 开一条**根 span**。默认名字固定为：

```text
<agent-name>.turn-run
```

`agent-name` 来自 `Agent.name`（空白压成 `-`，小写）。未命名则用 `agent`。

| 你写的 Agent | 根 Span Name（默认） |
| --- | --- |
| `Agent(name="movie-assistant")` | `movie-assistant.turn-run` |
| `Agent(name="Movie Bot")` | `movie-bot.turn-run` |
| `Agent()` 没有 `name` | `agent.turn-run` |

```python
from agno_harness import ObservabilityModule

# Agent(name="movie-assistant") → "movie-assistant.turn-run"
runtime.register_module(ObservabilityModule().bind_agent(agent))
```

- **不要**把 run_id / UUID 写进名字。Langfuse / OTel 按 Span Name 分桶，名字一变一桶，看板就废了。
- `run_id`、`thread_id`、`user_id`、`channel_id` 只进 **Attribute**。
- 只有要把几种会话拆开看时才传 `span_name=`（例如 DM vs 群）；回调本身也必须低基数。

---

## 3. 多维属性映射 (Multi-Dimensional Attribute Matrix)

`agno-harness` 严格对齐 OpenInference 与 OpenTelemetry 规范，每一次 Agent Run 都会自动注入下列高价值字段，便于在 Langfuse 中进行组合筛选：

| 追踪属性 (Attribute Key) | 含义与标准 | 过滤价值 |
| :--- | :--- | :--- |
| **`session.id`** | 统一会话 ID（Agno session ID / thread_id） | 查看同一会话的完整多轮对话 Trace 树 |
| **`user.id`** | 真实认证的用户主键 ID | 追踪特定用户的行为与故障回溯 |
| **`platform`** | 发起渠道（`web`, `teams`, `lark`, `cli`） | 对比不同终端渠道的使用频次与异常率 |
| **`deployment.environment`** | 运行环境（`production`, `staging`, `dev`） | 生产与测试流量物理隔离筛选 |
| **`tags` / `langfuse.tags`** | 自定义标签数组（如 `["vip", "billing"]`） | 业务维度筛选看板 |
| **`input.value`** | 当前轮次的用户输入 Prompt | 查看模型输入详情 |
| **`output.value`** | 最终输出给用户的文本内容 | 检查模型生成质量与合规 |
| **`agui.ttft_ms`** | 首字到达耗时（Time To First Token, 毫秒） | 关键核心性能体验指标 |
| **`agui.duration_ms`** | 整个推理交互总耗时（毫秒） | 吞吐与延迟分析 |
| **`agui.cards`** | 本轮次生成的结构化 UI 卡片数量 | 分析富交互功能渗透率 |
| **`agui.tool_calls`** | 本轮次触发的工具调用总次数 | 分析 Agent 规划复杂度与死循环倾向 |

---

## 4. 解决异步 Span 遗漏问题 (Patch Context Detach)

在 Python 复杂的 `asyncio` 事件循环、流式生成器（`async for`）与后台并发任务中，原生 OpenTelemetry 上下文经常因为跨协程切换而“断流”，导致子 Span 无法挂载在父 Span 之下。

`agno-harness` 内置了 `patch_context_detach()` 机制，在通道流转过程中自动保护上下文传递，确保子 Agent、工具调用、卡片解析的所有 Span 稳固地挂在同一个 Root Trace 下。
