# 01. 为什么需要 SmartCompressionManager，以及 `num_history_runs`

多轮长会话、Todo 规划、高频工具调用时，开发者常撞上：

1. **上下文超限或成本飙升**：全量历史与长工具结果让 Token 指数增长。
2. **原生机制机械失忆**：Agno 原生压缩同步打多次 LLM；滑动窗口直接丢掉早期历史。

## 1. 原生 CompressionManager 的缺陷

| 维度 | Agno 原生 | `SmartCompressionManager` |
| --- | --- | --- |
| 粒度 | 只压 `role: "tool"`，压不了 User / Assistant | 多轮交互与工具产物整体浓缩成 Checkpoint |
| 阻塞 | 每个工具结果各一次同步补全，流式卡 10–20s | 超限时一次 Checkpoint，通常 1–2s |
| KV Cache | 每次全新 Prompt，前缀缓存打穿 | 在已有消息末尾追加提示，前缀可复用 |
| 语义 | 孤立压单个工具，丢掉「用户要什么」 | 站在全篇总结意图、决策、事实、下一步 |
| 可观测 | 无专用 span | `context_compression` span，记录前后 Token |

## 2. 必须把 `num_history_runs` 设为 100

```python
agent = Agent(
    ...,
    add_history_to_context=True,
    num_history_runs=100,  # 不能是 0，也不能是 None
    compress_tool_results=True,
    compression_manager=compression_manager,
)
```

| 设置 | Agno 底层 | 实际历史 | 结论 |
| --- | --- | --- | --- |
| **`0`** | `last_n_runs <= 0` → `return []` | **0 轮** | 单轮无记忆，压缩器无从谈起 |
| **`None`** | `__init__` 把 `None` 改成 **3** | **硬截断 3 轮** | 第 4 轮丢掉早期约束；Token 往往够不到阈值，压缩器被饿死 |
| **`100`** | `runs[-100:]` | 最近 100 轮 | 记忆交给压缩器；达阈值出 Checkpoint，再修剪旧消息 |

`0` 会让 `AgentSession.get_messages` 直接返回空列表。`None` 不是「不限制」——Agno 强制默认 3。

设 100 **不会**把上下文撑爆。检查点自适应修剪：

```text
轮次 1–3：Token 2k → 6.8k < 7k → SKIP，保留完整对话
轮次 4：8.5k >= 7k → 生成 # CONTEXT CHECKPOINT，内存重组到 ~1.8k
轮次 5+：识别最近 Checkpoint，丢掉它之前的原始轮次
即使 num_history_runs=100，注入模型的仍是 Checkpoint + 近轮，约 2k
```

下一步：[02 检查点与配置](02-checkpoint-and-config.md)。
