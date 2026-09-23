# 01-foundations / 持久化与长任务

完整历史是 **两套库**，少一个就不完整。

| 库 | 记住什么 | 缺了会怎样 |
| --- | --- | --- |
| **Agno `db`**（`SqliteDb` / `PostgresDb`） | 下一轮模型上下文（`agno_sessions` / `agno_runs`） | UI 能回放，模型失忆 |
| **Harness `Stores`**（SQL `event_log` / `history_archive`） | 用户看过的流（frames、卡片、自定义事件） | 模型记得，刷新丢卡片 / 工具 |

Harness 已经是持久 SQL、Agno `db` 却缺失或内存时，`AgentRuntime` **直接失败**，除非显式 `allow_ephemeral_agno_db=True`。

Agno session 是给**下一轮模型**的，不是用户看过的流。刷新还要看见卡片 / 子面板，或 run 必须活过那条 HTTP，用两套存储 + `LongRunManager`。

前端 idle 重连 → [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md)。本系列只写路由与存储职责。

| 步 | 内容 |
| --- | --- |
| [01 双存储](01-dual-storage.md) | 热 log vs archive；mixin；Alembic；`X-Agui-Resume` |
| [02 长任务路由](02-longrun-routes.md) | `LongRunManager`、`/attach`、`/active`、`/abort`；ping vs Redis beat |
| [03 FAQ](03-faq.md) | 提问消失、空白气泡、Stop、`runCount` |
