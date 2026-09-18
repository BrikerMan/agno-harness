# 02. 检查点与配置

## 1. 运行时序

```text
Pre-flight → SmartCompressionManager.should_compress()
        Token < trigger → 跳过，进本轮 LLM
        Token >= trigger → acompress()
            1. 复用历史前缀 (KV Cache)
            2. 生成结构化 Checkpoint
            3. 原地重组 messages
        → 本轮 LLM → PostRunHook → attach_checkpoint_to_session()
```

预检用本地 `cl100k_base`，统计消息与 Tools JSON Schema，通常 `< 1ms`。`debug_mode=True` 会打：

```text
INFO [SmartCompression] Pre-LLM Check: 11 msgs, 6,877 total tokens
     (msgs: 4,680 + tools: 2,197), trigger limit: 7000 -> SKIP
```

达阈值时，把 `DEFAULT_IN_CONTEXT_CHECKPOINT_PROMPT` 作为**最后一条临时 user 消息**追加在已有会话后面，再 `aresponse`。前面的 messages 与刚缓存的 Prefix 一致，vLLM / 兼容网关可以打中 Prompt Cache。

## 2. Checkpoint 必须写什么

`# CONTEXT CHECKPOINT` 按这五块提炼：

1. **User Intent & Constraints**：目标、交付格式、硬限制。
2. **Key Decisions & Rationale**：已达成共识与选型原因。
3. **Discovered Facts & Ground Truth**：禁止「已查了天气」这种空话；必须写精确数值与实体。
4. **Completed Actions & Findings**：调过的工具、产物、异常。
5. **Active State & Immediate Next Steps**：进度，以及紧接着必须做的一步。

重组：保留第一条 System；注入 `[Context Checkpoint]`（**user**）+ Assistant 确认；保留当前 Active Turn 的 User / Tool；丢掉更早轮次。

为什么是 `user` 不是 `system`：不少 OpenAI 兼容端要求 `system` **只能出现在 index 0**。中间插 `system` 会 400。

## 3. 接入模板

```python
import os
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai.like import OpenAILike
from agno_harness import AgentRuntime, SmartCompressionManager, make_checkpoint_hook

model = OpenAILike(
    id="qwen-plus",
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL"),
)
db = SqliteDb(db_file="data/sessions.db")

trigger_tokens = int(os.getenv("COMPRESSION_TRIGGER_TOKENS", "50000"))
compression_manager = SmartCompressionManager(
    model=model,
    trigger_token_limit=trigger_tokens,
    min_messages=4,
    debug_mode=True,
)

agent = Agent(
    name="researcher",
    model=model,
    db=db,
    tools=[...],
    instructions="...",
    add_history_to_context=True,
    num_history_runs=100,
    compress_tool_results=True,
    compression_manager=compression_manager,
    markdown=True,
    telemetry=False,
)

runtime = AgentRuntime(agent=agent, db=db)
runtime.on_post_run(make_checkpoint_hook(runtime))
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `model` | `None` | 生成 Checkpoint 的模型，通常复用主模型 |
| `trigger_token_limit` | `12000` | 触发阈值，见下表 |
| `min_messages` | `4` | 条数过少不压 |
| `compress_tool_results` | `True` | `False` 则完全跳过 |
| `debug_mode` | `False` | 打印预检与收益 |
| `encoding_name` | `"cl100k_base"` | tiktoken |
| `checkpoint_instructions` | `None` | 自定义提示词 |

## 4. 阈值矩阵

标称 1M 上下文 **不等于** 多轮 Agent 可以无限滚雪球。50k 左右是智商、延迟、显存并发、账单的平衡点：注意力稀释、KV Cache 显存墙、按输入 Token 累计计费、超长 Prefill 的 TTFT。

| 场景 | 推荐 `trigger_token_limit` |
| --- | --- |
| 128k–1M 旗舰（生产） | **50,000**（可 50k–80k） |
| 128k 标配 | **50,000**（40k–60k） |
| 高频 Router / SubAgent | **20,000**（15k–25k） |
| 本地看卡片效果 | **7,000**（5k–8k） |

用环境变量配，不要写死。压缩 span：`compression.original_tokens` / `compacted_tokens` / `saved_tokens` / `messages_before` / `messages_after` / `elapsed_seconds`。

## 5. 前端折叠卡

压缩触发时会出 `CUSTOM`（或 StreamUI 块）带上前后 Token。前端画一张**折叠检查点卡**，不要把整份 Checkpoint 当普通气泡刷屏。

- 默认一行：`⚡ In-Context Checkpoint: 12.4k → 2.1k (-83%)`。
- 展开三个数：压缩前 / 压缩后 / 已节省，下面用 Markdown 渲染完整 `# CONTEXT CHECKPOINT`。
- 刷新后同一套 `applyEvent` 还能打开这张卡（事件进了 archive）。
- 本地想尽快看到卡：把 `COMPRESSION_TRIGGER_TOKENS` 调到 `5000`–`7000`。生产仍用 50k。

下一步：[03 封口、FAQ、Web 怎么确定没问题](03-seal-and-faq.md)。
