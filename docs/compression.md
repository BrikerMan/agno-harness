# 智能上下文压缩与检查点 (SmartCompressionManager)

在多轮长会话、复杂任务规划（Todo Workflow）或高频工具调用的 Agent 场景中，随着上下文迅速膨胀，开发者常面临两大严峻挑战：
1. **上下文超限或成本飙升**：全量历史与长工具结果导致 Token 消耗指数级增长，响应变慢。
2. **传统机制的机械失忆与卡死**：Agno 原生压缩策略导致频繁的同步 LLM 调用与 KV Cache 击穿；而原生历史滑动窗口则直接粗暴丢弃早期历史，导致严重失忆。

`agno-relay` 提供了专为企业级生产设计的 **`SmartCompressionManager`** 与 **In-Context Checkpointing** 闭环机制。

参考实现：
- 核心压缩器：[`src/agno_relay/runtime/compression.py`](../src/agno_relay/runtime/compression.py)
- 会话闭环与历史修剪：[`src/agno_relay/runtime/closure.py`](../src/agno_relay/runtime/closure.py)
- 前端折叠检查点卡片：[`examples/demo/frontend/src/components/chat/ContextCompressionCard.tsx`](../examples/demo/frontend/src/components/chat/ContextCompressionCard.tsx)
- Demo 完整配置示例：[`examples/demo/backend/app/agent_setup.py`](../examples/demo/backend/app/agent_setup.py)

---

## 1. 为什么需要 SmartCompressionManager？

### 1.1 Agno 原生 CompressionManager 的缺陷

Agno 原生提供了一个 `CompressionManager`，但其内部机制在生产流式和复杂 Agent 下存在明显缺陷：

| 维度 | Agno 原生 CompressionManager | agno-relay 的 SmartCompressionManager |
| :--- | :--- | :--- |
| **压缩粒度与范围** | 仅压缩 `role: "tool"` 的输出；**无法压缩对话消息（User/Assistant）**，多轮交互依然爆 Token | **全量上下文 In-Context 提炼**：将过去多轮交互与所有工具产物整体浓缩为高密度 Checkpoint |
| **LLM 耗时与阻塞** | **单工具独立同步调用**：若有 5 个工具结果，连续发起 5 次同步 LLM 补全，导致前端流式输出卡顿 10~20 秒 | **单次追加式 LLM 补全**：仅在总 Token 超限时发起单次 Checkpoint 总结，通常 1~2 秒内完成 |
| **KV Cache 命中率** | **彻底击穿 Cache**：每次为单个工具组装全新 Prompt，无法利用大模型前缀缓存，不仅极慢且成本成倍上升 | **100% KV Cache 命中**：直接在已有消息流末尾追加 Checkpoint 提示词，大模型复用已有上下文 KV 缓存 |
| **信息割裂度** | 孤立压缩单个工具输出，丢失了“用户最初要什么”、“各工具结果之间如何关联”的全局语义 | **全局连贯语义**：大模型站在全篇视角总结用户意图、关键决策、确切事实和当前下一步 |
| **可观测性** | 无专用追踪埋点，终端信息模糊 | 原生集成 OpenTelemetry / Langfuse，发射 `context_compression` span，记录压缩前后 Token 与耗时 |

---

## 2. 为什么必须将 `num_history_runs` 设为 100？不能设为 0 或 None？

这是使用 `SmartCompressionManager` 最关键的一项配置，也是最容易踩坑的配置点。

```python
agent = Agent(
    ...,
    add_history_to_context=True,
    num_history_runs=100,  # 务必设为 100（或更大值），绝不能是 0，也绝不能是 None！
    compress_tool_results=True,
    compression_manager=compression_manager,
)
```

### 2.1 三种设置的底层行为对照（0 vs None vs 100）

查看 Agno 的底层源码（`Agent.__init__` 与 `AgentSession.get_messages`）：

| 设置值 | Agno 底层解析结果 | 实际读取的历史轮数 | 对上下文与记忆的影响 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **`0`** | `if last_n_runs <= 0: return []` | **0 轮（0 条消息）** | **彻底无记忆（单轮模式）**。每轮对话都像第一次开始，完全丢失上文，压缩器更无从谈起。 | ❌ **绝对不可用** |
| **`None`**（或不填） | `if num_history_runs is None: self.num_history_runs = 3` | **强制被劫持为 3 轮** | **硬截断失忆**。从第 4 轮起机械丢弃早期历史；且 3 轮消息 Token 往往无法触达压缩阈值，压缩器永远无法触发。 | ❌ **严重陷阱** |
| **`100`**（或更大） | `runs = runs[-100:]` | **最近 100 轮（全量历史）** | **记忆完全由 SmartCompressionManager 接管**。累积达阈值自动触发 Checkpoint，后续轮次自动修剪冗余旧消息。 | ✅ **标准推荐配置** |

### 2.2 为什么设为 0 会彻底失效？

在 Agno 的 `AgentSession.get_messages` 源码中：
```python
# Filter by last_n_runs before applying message limit
if last_n_runs is not None:
    if last_n_runs <= 0:
        return []
    runs = runs[-last_n_runs:]
```
一旦传入 `num_history_runs=0`，Agno 会直接返回空列表 `[]`。这会导致 Agent 在任何一轮交互中**完全读取不到任何前序历史对话**，直接退化为无状态的单轮会话。

### 2.3 为什么设为 None 会被暗中劫持？

在 Agno 的 `Agent.__init__` 源码中：
```python
if self.num_history_messages is None and self.num_history_runs is None:
    self.num_history_runs = 3  # Agno 强行默认只保留最近 3 轮！
```
- 很多开发者以为传 `None` 是“不作限制、加载全部”，但 Agno **把 `None` 强行覆盖为了 `3`**。
- 这会导致机械滑动窗口硬截断：当对话到达第 4 轮时，第 1 轮（用户最初的需求、核心约束）就被硬生生丢弃了；
- 更致命的是：因为只有 3 轮消息进入内存，总 Token 往往停留在 2,000 ~ 4,000，**永远无法达到 `SmartCompressionManager` 的触发阈值（如 7,000 或 12,000）**，导致智能压缩器被“活活饿死”。

### 2.4 为什么设为 100 不会把上下文撑爆？（自适应修剪机制）

有开发者担心：“设为 100 轮，如果对话很长，会不会让 Token 暴涨撑爆大模型？”
**答案是：完全不会！** 因为 `agno-relay` 实现了**检查点自适应修剪机制**：

```text
[轮次 1 ~ 3]
历史正常累积（Token: 2k -> 5k -> 6.8k < 7k）
  └─ Pre-LLM Check: SKIP (未达阈值，保留完整对话与工具细节)

[轮次 4]
历史累积超过阈值（Token: 8.5k >= 7k）
  └─ ⚡ TRIGGERED: SmartCompressionManager 触发
  └─ In-Context Checkpointing 生成高密度 # CONTEXT CHECKPOINT
  └─ 内存中消息重组：[System, Checkpoint, Current Turn Messages] (8.5k -> 1.8k，节省 78%)
  └─ Post-Run Hook 将 Checkpoint 存入 Session Database 的 metadata

[轮次 5 及后续]
AgentSession.get_messages() 读取历史：
  └─ install_sealed_history_hook 识别到最近生成的 Checkpoint
  └─ 自动修剪掉 Checkpoint 之前的前 4 轮冗余原始消息
  └─ 仅加载 [Checkpoint] + [轮次 4 的问答] + [后续轮次]
  └─ 即使 num_history_runs=100，实际注入模型的 Token 依然保持轻量（~2k），兼具超长记忆与极低开销！
```

---

## 3. 核心机制与架构设计

### 3.1 架构全景流程图

```text
                       Agent 运行准备开始 (Pre-flight)
                                     │
                                     ▼
                SmartCompressionManager.should_compress()
                                     │
                    ┌────────────────┴────────────────┐
          (总 Token < trigger_limit)        (总 Token >= trigger_limit)
                    ▼                                 ▼
              [跳过压缩]                      ⚡ acompress() 触发
            正常进入 LLM 推理              ┌───────────────────────────┐
                                          │ 1. 复用历史前缀 (KV Cache) │
                                          │ 2. 生成结构化 Checkpoint   │
                                          │ 3. 内存重组 messages 列表  │
                                          └─────────────┬─────────────┘
                                                        ▼
                                              进入当前轮次 LLM 推理
                                                        │
                                                        ▼
                                              PostRunHook (运行时完成)
                                                        │
                                                        ▼
                                         attach_checkpoint_to_session()
                                          (Checkpoint 存入数据库元数据)
```

### 3.2 零延迟精确 Token 预检 (tiktoken)

无需任何网络调用，`SmartCompressionManager` 使用本地 `cl100k_base` 编码：
- 精确计算对话消息列表（System、User、Assistant、Tool 等角色的 Content、Name、ToolCall ID 与入参）。
- 精确统计当前挂载的所有 Tools 的 JSON Schema 定义所占用的 Token 开销。
- 统计开销仅需 `< 1ms`，在 `debug_mode=True` 下会打印详细的透明日志：
  ```text
  INFO [SmartCompression] 🔍 Pre-LLM Check: 11 msgs, 6,877 total tokens (msgs: 4,680 + tools: 2,197), trigger limit: 7000 -> SKIP (6,877 < 7,000)
  ```

### 3.3 In-Context Checkpointing 与 100% KV Cache 命中

当达到压缩阈值时，`SmartCompressionManager` 不会单独开辟隔离上下文，而是将 `DEFAULT_IN_CONTEXT_CHECKPOINT_PROMPT` 作为临时的最后一条消息直接追加在已有会话后面：

```python
checkpoint_request_messages = list(messages) + [
    Message(role="user", content=prompt),
]
response = await model_instance.aresponse(messages=checkpoint_request_messages)
```
- **KV Cache 命中原理**：因为前面的 `messages` 与大模型刚刚缓存的 Prefix 完全一致，服务端（如 vLLM、DeepSeek、Anthropic、OpenAI）能够实现 **100% Prompt Cache 命中**。
- Checkpoint 的生成速度极快（通常 1~2 秒），成本相较于全量新 Prompt 降低 70%~90%。

### 3.4 高密度 Checkpoint 结构规范

生成的 `# CONTEXT CHECKPOINT` 遵循极其严格的信息提炼规范：

1. **User Intent & Constraints**：用户核心目标、交付格式（卡片、表格、文本）、硬性限制。
2. **Key Decisions & Rationale**：已达成的共识、技术/架构选型与原因。
3. **Discovered Facts & Ground Truth（最核心）**：
   - **绝对严禁模糊概括**（如“已查询了天气”或“已读取了文件”）。
   - **必须记录精确数值与实体**（例如：`东京: 25°C 晴; 大阪: 23°C 晴; 札幌: 19°C 多云; 路径: /app/config.json; 用户 ID: 1082`）。
4. **Completed Actions & Findings**：已调用的工具、关键产物、处理过的异常。
5. **Active State & Immediate Next Steps**：当前执行进度、刚完成的事项，以及**为了完成任务紧接着必须执行的下一步**。

### 3.5 消息列表就地重组 (In-Place Reorganization)

Checkpoint 生成后，`reorganize_messages_with_checkpoint` 立即在内存中原地替换 `messages[:]`：
- 保留第一条 System Instruction。
- 注入 `[Context Checkpoint]\n...`（作为 User 角色）与 `Understood. I have recorded the context checkpoint.`（作为 Assistant 确认）。
- **保留当前执行中轮次（Active Turn）**的 User 消息以及最近的 Tool Calls / Tool Results。
- 抛弃已被 Checkpoint 提炼过的更早轮次。
- Agent 在重组后的轻量上下文上继续执行当前任务，过程丝滑无感知。

---

## 4. 快速接入与最佳实践

### 4.1 标准后端配置模板

```python
import os
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai.like import OpenAILike
from agno_relay import (
    AguiRuntime,
    SmartCompressionManager,
    make_checkpoint_hook,
)

# 1. 初始化模型与持久化 DB（现代主流模型如 Qwen 2.5/3、DeepSeek-V3/R1、Claude 3.7 Sonnet 等）
model = OpenAILike(
    id="qwen-2.5-72b-instruct",
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL"),
)
db = SqliteDb(db_file="data/sessions.db")

# 2. 配置 SmartCompressionManager
# 对于现代旗舰长上下文模型（标称 128k / 200k / 1M），推荐生产环境设置 40,000 ~ 60,000（如 50k）；
# 本地开发/Demo 可设置 6,000 ~ 8,000 便于观察卡片交互
trigger_tokens = int(os.getenv("COMPRESSION_TRIGGER_TOKENS", "50000"))
compression_manager = SmartCompressionManager(
    model=model,
    trigger_token_limit=trigger_tokens,
    min_messages=4,
    debug_mode=True,  # 打印透明的预检与压缩收益日志
)

# 3. 配置 Agno Agent
agent = Agent(
    name="researcher",
    model=model,
    db=db,
    tools=[...],
    instructions="...",
    add_history_to_context=True,
    # 【核心配置】必须指定为一个大数字（如 100），交给 SmartCompressionManager 自主决策
    num_history_runs=100,
    compress_tool_results=True,
    compression_manager=compression_manager,
    markdown=True,
    telemetry=False,
)

# 4. 初始化 Runtime 并注册 Checkpoint 持久化 Hook
runtime = AguiRuntime(agent=agent, db=db)
runtime.on_post_run(make_checkpoint_hook(runtime))
```

### 4.2 配置参数详解

| 参数名 | 类型 | 默认值 | 作用与建议 |
| :--- | :--- | :--- | :--- |
| `model` | `Model` | `None` | 执行 Checkpoint 生成的模型实例，通常复用 Agent 的主模型。 |
| `trigger_token_limit` | `int` | `12000` | 触发检查点压缩的上下文 Token 阈值。详见 4.3 阈值选型矩阵。 |
| `min_messages` | `int` | `4` | 触发压缩的最小消息数。防止消息条数过少但单条超长时过早触发。 |
| `compress_tool_results` | `bool` | `True` | 是否启用压缩管理。若为 `False` 则完全跳过。 |
| `debug_mode` | `bool` | `False` | 是否在控制台输出详细的 Token 预检与 Checkpoint 收益日志。开发阶段强烈建议开启。 |
| `encoding_name` | `str` | `"cl100k_base"` | `tiktoken` 编码名称，适用于 GPT-4、GPT-3.5、Qwen 等绝大多数主流模型。 |
| `checkpoint_instructions`| `str \| None` | `None` | 自定义 Checkpoint 提示词。若为 `None` 则使用内置的 `DEFAULT_IN_CONTEXT_CHECKPOINT_PROMPT`。 |

---

### 4.3 阈值选型矩阵：现代超长上下文模型（128k / 256k / 1M+）的最佳实践

在现代大模型生态中，旗舰模型已普遍支持 128k、256k 甚至 **1M（100 万）~ 2M** 的超长上下文（例如 **Qwen 2.5/3 系列、DeepSeek-V3/R1、Gemini 2.5/3、Claude 3.7 Sonnet、Kimi** 等）。

很多开发者因此产生疑问：“**既然模型标称 1M 上下文，我们为什么还要在 50,000（50k）左右进行压缩？**”

---

#### 核心认知：为什么 1M 上下文模型依然需要在 50k 处压缩？

超长上下文（1M）解决了“单次塞入百万字大文档能不能读”的问题，但**完全不等于适合在 Multi-Turn Agent 闭环中放任上下文无限滚雪球**。生产实践表明，将压缩阈值设在 **50k 左右** 是综合智商、延迟、成本与并发的黄金平衡点：

##### 1. 注意力稀释（Attention Dilution）与指令漂移（Instruction Drift）
- **长文本跑分 ≠ Agent 自主决策**：简单的“大海捞针”（NIAH）单行事实检索跑分，不代表模型在复杂多步 Agent 任务中能稳定保持高智商。
- 在真实的多轮对话和 Tool Calling 过程中，早期积累的数十个无用工具原始输出、调用重试报错、冗余中间 JSON 会严重**稀释自注意力权重（Self-Attention Weights）**。
- 当上下文膨胀到 100k 以上时，模型极易出现**指令漂移**、忘记 System Prompt 设定的核心约束、忽略动态 Todo 规划清单，甚至发生工具参数幻觉。
- **50k 处生成 Checkpoint**，相当于大模型在工作台堆满杂物前进行了一次“深度认知整理与索引化”，让大脑永远工作在 95%+ 的巅峰智商水位。

##### 2. 服务端显存墙（KV Cache Memory Wall）与并发坍塌
- 现代私有化推理框架（vLLM / SGLang）使用 PagedAttention 管理 KV Cache。
- 算一笔真实的硬件显存账：对于 70B+ 规模的旗舰模型，即使使用 FP8 KV Cache，**单并发用户维持 100k 上下文就需要吞掉数 GB 显存**；若放任用户会话跑向 300k~1M，**单个用户就能吃满整张甚至多张 80GB H100/H800 的显存**！
- 显存被长上下文吃满后，服务的并发请求承载量（Concurrency）将直接断崖式归零。
- 通过在 50k 处压缩并回落至 2k~4k，单张 GPU 的并发承载力可提升 **10 ~ 20 倍**！

##### 3. 真实 API 账单（Token Economics）
- 即使商业 API 支持 1M 上下文，**收费依然是按输入 Token 数逐轮累计计算**。
- 若放任一个 30 轮的复杂长任务滚雪球：第 5 轮 20k、第 15 轮 80k、第 25 轮 200k……累积的输入 Token 总量将突破数千万，单次任务成本高达数十美元。
- 在 50k 处触发 Checkpoint，每当达到 50k 就自动浓缩回 3k 左右，使得后续几十轮依然在超低水位运行，**API 账单整体降低 80% ~ 90%**！

##### 4. 首字延迟（TTFT）与交互假死感
- 尽管现代推理平台支持 Prompt Caching，但在多轮 Agent 复杂的工具分支下，一旦发生跨实例负载调度或微小前缀抖动导致 Cache Miss，对 200k~500k 的超大文本做 Prefill 计算需要花费 **10 ~ 30 秒**。
- 用户端会感受长达半分钟的假死无响应；而将阈值锁定在 50k 以内，即便冷启动 Prefill 也能在 1~2 秒内迅速吐出首字。

---

#### 现代主流模型压缩阈值推荐矩阵

| 模型体系与典型上下文 | 推荐 `trigger_token_limit` | 深度考量与适用场景 | 预期压缩收益 |
| :--- | :--- | :--- | :--- |
| **现代超长上下文 / 1M+ 旗舰模型**<br>• Qwen-2.5-1M / Qwen 3 (1M)<br>• Gemini 2.5 / 3.0 Pro/Flash (1M~2M)<br>• Kimi (Moonshot 1M/2M)<br>• Claude 3.7 Sonnet (200k/1M)<br>• DeepSeek-V3 / R1 (128k/1M) | **`50,000 ~ 80,000`**<br>（**黄金基线推荐：`50,000`**） | **兼顾海量事实吞吐与极致推理敏锐度**。<br>50k~80k 能够从容容纳 10~20 轮深度工具调用与大篇幅网页提取；在注意力稀释发生前精准收敛，保证模型指令遵循与 Todo 规划 100% 精确。 | 一次性从 50k~80k 浓缩至 2k~4k，**节省 92%~96% Token**，KV Cache 命中极速完成。 |
| **现代标配长上下文模型**<br>（128k ~ 256k）<br>• Qwen-2.5-72B (128k)<br>• DeepSeek-V3 / R1 (128k)<br>• GPT-4o / GPT-5 (128k)<br>• Llama-3.3-70B (128k) | **`40,000 ~ 60,000`**<br>（**黄金推荐值：`50,000`**） | 处于模型总容量的 **30% ~ 45%** 黄金推理区间，彻底远离上下文上限，为大模型输出超长思维链（Reasoning Tokens）与大卡片预留充裕空间。 | 从 50k 浓缩至 2k~3k，**节省 94%+ Token**。 |
| **高频轻量 / 调度 Router / SubAgent**<br>（轻量任务流转、高并发客服） | **`15,000 ~ 25,000`**<br>（**典型值：`20,000`**） | 保持极轻量上下文，单机并发最大化（高 RPS），将单会话平均成本压至几分钱。 | 长期稳定维持在 1k~2k 超低水位线运行。 |
| **本地开发 / 测试与 Demo 演示**<br>（前端卡片效果验证） | **`5,000 ~ 8,000`**<br>（**Demo 默认：`7,000`**） | 便于在 2~3 轮对话或几次工具调用内**快速触达阈值**，验证 `<ContextCompressionCard>` 折叠卡片渲染、Token 统计与断点恢复。 | 快速验证功能闭环。 |

> 💡 **生产环境建议**：通过环境变量灵活配置（如 `COMPRESSION_TRIGGER_TOKENS=50000`），避免将数值硬编码在业务代码中。开发测试时可随手切换为 `7000` 便于观察。

---

## 5. 前端展示与链路可观测性

### 5.1 OpenTelemetry / Langfuse 链路追踪

每次压缩触发时，`SmartCompressionManager` 会自动开启一个名为 **`context_compression`** 的独立 OTel Span，并在属性中注入：
- `compression.original_tokens`: 压缩前总 Token 数
- `compression.compacted_tokens`: 压缩后总 Token 数
- `compression.saved_tokens`: 节省的 Token 数量
- `compression.messages_before`: 压缩前消息条数
- `compression.messages_after`: 压缩后消息条数
- `compression.elapsed_seconds`: Checkpoint 生成耗时（秒）

在 Langfuse / Phoenix 中可清晰追溯每一次长对话发生的压缩收益与耗时分布。

### 5.2 前端折叠检查点卡片 (`ContextCompressionCard`)

前端可以在对话流中直接渲染折叠卡片：
- 默认展示紧凑状态：`⚡ In-Context Checkpoint: 12.4k → 2.1k (-83%)`。
- 展开后展示三个统计方块（原始上下文、压缩后上下文、已节省 Token），并以优雅的 Markdown 渲染完整的 `# CONTEXT CHECKPOINT` 记忆快照，方便用户随时查看 Agent 的记忆切片。

---

## 6. 常见问题与排查 (FAQ)

### Q1: 为什么控制台频繁打印 `SKIP (6,877 < 7,000)`？
这是正常现象。`debug_mode=True` 会在每次 LLM 交互前执行预检并报告当前 Token 存量。只有在多轮对话或复杂工具执行后累积突破 `trigger_token_limit` 时，才会触发压缩。如果希望在更少轮次内测试压缩效果，可以把环境变量 `COMPRESSION_TRIGGER_TOKENS` 临时调低（如 `5000`）。

### Q2: 能否把 `num_history_runs` 设为 `None`？
**绝对不能**。在 Agno 内部，若 `num_history_runs` 为 `None`，会被默认赋值为 `3`。这将导致第 4 轮开始丢失早期历史，且由于上下文永远不超过 3 轮，压缩器可能永远无法触发。务必显式传入 `num_history_runs=100`。

### Q3: 为什么压缩后的 Checkpoint 作为 `user` 角色注入而不是 `system`？
部分遵循 OpenAI 协议的模型提供商（如 vLLM、Qwen-OpenAI 兼容端等）强制要求：`system` 角色消息**只能出现在整个 messages 列表的最开头（index 0）**。如果在消息流中间插入 `system` 角色消息，API 会直接抛出 `400 Bad Request`。因此，Checkpoint 以 `[Context Checkpoint]` 的 User 消息搭配 Assistant 的确认注入，在保证所有模型兼容性的同时完美传递上下文。
