# 03. 封口与 FAQ

刷新、断网、进程被杀时，历史里可能留下「Tool Call Started 但无 Result」。下一轮模型会因消息顺序不合法崩溃。

```python
from agno_harness.runtime.closure import close_dangling_tool_calls, seal_session_run

await seal_session_run(db, session_id, run_id, reason="Stopped by user")
```

密封会：

1. 扫描没有对应 Tool Result 的悬挂调用；
2. 插入合成取消记录（例如客户端断开）；
3. 把该 run 标成可继续的终态；
4. 保证下一轮发给模型的顺序符合 OpenAI 交替契约。

Agno v3 下的持久化：封口会修改 run（`metadata["sealed"]`、补齐的 tool 结果消息、已有的部分文本），并用 `db.upsert_run(...)` 写回。`upsert_session` 已经不再保存 run，只经由它保存的修改会丢，下一轮仍会撞上悬挂的 tool call。Checkpoint（`attach_checkpoint_to_session`）走同一条路径。存储里真实的状态（`CANCELLED` / `ERROR`）不会被改写；历史 hook 只在拼下一轮提示词时，才把已封口的 run 当作对话的一部分。

Agno 在自己的清理路径里写入被中断的 run，可能晚于 harness 开始封口。封口会重读最多约半秒，等不到才回退到最新一个 run。

挂在 post-run / 异常退出路径上。与压缩正交：压缩管 Token，封口管脏 tool call。

## FAQ

### Q1: 为什么控制台频繁打印 `SKIP (6,877 < 7,000)`？

正常。`debug_mode=True` 每轮预检。只有累积突破 `trigger_token_limit` 才压。想少轮次看到卡片，把 `COMPRESSION_TRIGGER_TOKENS` 临时调到 `5000`。

### Q2: 能不能把 `num_history_runs` 设成 `None`？

**不能。** Agno 会改成 `3`。第 4 轮丢掉早期历史，压缩器可能永远不触发。显式写 `100`。详见 [01](01-why-and-num-history.md)。

### Q3: 为什么 Checkpoint 用 `user` 角色？

兼容网关要求 `system` 只能在列表开头。中间插 `system` 会 400。用 `[Context Checkpoint]` user + Assistant 确认。

### Q4: 压缩后下一轮还记得用户最初的约束吗？

记得——写在 Checkpoint 的 Intent / Constraints / Ground Truth 里，不靠原始第 1 轮气泡。若摘要丢了硬限制，改 `checkpoint_instructions`，不要把 `num_history_runs` 改回 `None`。

## Web 怎么确定没问题

本地先把阈值调到 `7000`，`debug_mode=True`，`num_history_runs=100`。

| # | 操作 | 必须看到 |
| --- | --- | --- |
| 1 | 连聊 / 打工具直到超过阈值 | 日志从 `SKIP (x < 7000)` 变成一次 `TRIGGERED`；前端出折叠卡 `ak → bk (-%)`，不是又一篇长气泡 |
| 2 | 点开卡 | 能读到 Intent / Ground Truth 里的**具体数字和路径**，不是「已查询了天气」 |
| 3 | 再问一句「我最开始要求你做什么」 | 模型答得上第 1 轮约束；不要靠侧栏里那条已被修剪的原文气泡 |
| 4 | 刷新 | 检查点卡还在；`GET /api/v1/threads/{id}/frames` 回放能展开同一份摘要 |
| 5 | 故意设 `num_history_runs=None` 对照（仅本地） | 第 4 轮开始丢早期约束，且可能永远 `SKIP`——用来确认 **100 才对** |
| 6 | Langfuse / OTel | 有 `context_compression` span，带 `original_tokens` / `saved_tokens` |

封口另测：中途断掉一次带工具的 run，下一轮不应再报 `Invalid message order`。
