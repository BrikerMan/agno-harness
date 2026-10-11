# 04. Agno v3 采纳笔记

哪些 Agno v3 能力值得在 agno-harness 里采纳，哪些不值得。这一页是设计笔记，不是待办清单：除非明确写了已落地，否则都没有实现。AgentOS 的能力（持久化队列、`user_isolation`、JWT、`GET /models`）不在范围内，因为 harness 自带 FastAPI 和 AG-UI 层。

0.3 已经落地的：

- 封口和 checkpoint 通过 `upsert_run` 持久化（[封口](../02-interactions/04-compression-and-sealing/03-seal-and-faq.md)）。
- 面向 v2 数据库的 `agno-harness db migrate-agno`（[双存储](../01-foundations/05-persistence-and-longruns/01-dual-storage.md)）。
- `WorkspaceToolkit` 默认拒绝凭据文件。

## 值得做

### Guardrails（`agno.guardrails`）

`PIIDetectionGuardrail`、`PromptInjectionGuardrail`、`OpenAIModerationGuardrail` 通过 `Agent(pre_hooks=[...])` 挂载，命中时抛 `InputCheckError`。harness 目前不做任何输入校验，chime-in 策略只决定群消息要不要回复。

设想：做一个 `Module`（或默认 pre-hook），把 `InputCheckError` 映射成带安全提示语的 AG-UI `RUN_ERROR` 帧，并把触发原因写入审计 sink。内置的 PII 正则偏英文，中文手机号和身份证号要通过 `custom_patterns` 补充。

### 大工具结果卸载（`Agent(offload_tool_results=True)`）

超过阈值的工具结果写进 AgentFS，上下文里只留预览、大小和 `result_id`，模型用 `read_result` / `search_result` 回读。无损，也不额外调用模型。需要 `SqliteDb` 或 `PostgresDb`。

它和 `SmartCompressionManager` 互补：先卸载最大的消息，checkpoint 就触发得更少。采纳之前要确认：多出来的工具调用在 AG-UI 流里渲染是否合理；`<prefix>_tool_results` 表是否被表前缀覆盖（已覆盖）。

## 值得评估

- **`tool_call_limit`、`retries` / `exponential_backoff`、`tool_hooks`。** harness 没有 run 级的工具调用上限，也没有 run 级重试。上限能挡住死循环的 agent；`tool_hooks` 是统一做工具耗时、审计和脱敏的位置。
- **`@approval`。** `required` 阻塞到处理完毕，`audit` 只留记录。harness 的 HITL 已经能通过 `requirements` 暂停和恢复；新增的是审计类型，可以接到 sink 上。
- **`LearningMachine`。** 用户画像、用户记忆、会话上下文、实体记忆、学到的知识和决策日志，模式有 `ALWAYS`、`AGENTIC`、`PROPOSE`。除了 checkpoint，harness 没有跨会话记忆。`user` 命名空间的实体记忆在 v3 里被重新生成了键（见 Agno 迁移指南），启用之前先做迁移。
- **每次读取都带 `user_id`。** Agno v3 的 `get_session` 和 `get_runs` 都按 `user_id` 过滤。需要审查 `ThreadService`，让每一处读 session 和 run 的调用都传它。

## 保持现状

- **`SkillManager`。** 它会按卡片目录校验卡片声明，并即时注入卡片 schema；Agno 的 `Skills` 两样都不做。保持 `SKILL.md` frontmatter 兼容，让同一个文件两边都能加载。
- **`WorkspaceToolkit`、`TodoToolkit`、子 agent、压缩、封口。** 它们和 AG-UI 流、卡片协议绑在一起；Agno 的对应物只到模型层。
- **`followups` 和 `enable_session_summaries`。** 可以替换手写的标题和追问，但现有实现够用。

## 需要留意的开销

线程存储为空时，`ThreadService.get_sessions` 会回退到 Agno 的 session。在 v3 上每个 session 都会从 runs 表带出 run，因为侧边栏要标题和 `runCount`。如果这条回退路径变热，改用 `get_runs(session_id=..., limit=...)` 批量读取。
