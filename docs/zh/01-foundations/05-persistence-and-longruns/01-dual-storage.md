# 01. 双存储

完整历史是 **Agno `db` + harness `Stores`**，谁也替不了谁。

- **Agno `db`**（`Agent` 和 `AgentRuntime` 上的 `SqliteDb` / `PostgresDb`）— 下一轮模型消息。缺了它，frames 在盘上也没用，模型每轮从零开始。
- **Harness `Stores`** — 用户看过的流。缺了它，`/threads/{id}/frames` 还原不了当时的卡片 / 工具 / 思考。

Harness 已是持久 SQL、Agno `db` 却缺失或内存时，`AgentRuntime` 抛 `StoragePairingError`，除非 `allow_ephemeral_agno_db=True`。

Harness 内部还有一层拆分：

- **热 log**（`RedisRunEventLog`，或进程内 stand-in）— 每个 AG-UI delta、可阻塞 tail、TTL。`?long-run=1` 和 `GET /runs/{id}/attach` 读它。不要用 SQL 装 token。
- **History archive**（SQL）— 结束后合成的事件列表。连续 content delta 折成一帧。`GET /threads/{id}/frames` 走这里。

底座不替你建表。用 mixin 拼到你的 `Base`，自己 `create_all` / Alembic。

```python
from agno_harness.stores import (
    DefaultRelayBase,
    RunArchiveMixin,
    RunFrameMixin,
    RunRecordMixin,
    SessionRecordMixin,
    Stores,
    SQLAlchemyActionStore,
)
```

`Stores` 上 `event_log` 与 `event_stream` 分开：可以只有 SQL log（history），再加上 Redis 才是 live。`resume_mode` 由此算出。

| 配了什么 | `X-Agui-Resume` | 行为 |
| --- | --- | --- |
| 没有 | `none` | 连接掉 = run 停 |
| 只有 `event_log` | `history` | 后台跑完；不能句中 follow |
| 再加上 `event_stream`（Redis 或同实现） | `live` | `after=` 接到句中 |

跨进程 live 需要真 Redis。`memory://` 只服务本进程。写 log 失败标 `unrecordable`，不杀流。

两种 cursor 禁止混用：SSE `id:` = 单 run log offset（给 **attach**）；frames id = `{runId}:{paddedOffset}`（只给 `/frames`）。见 [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md)。

下一步：[02 长任务路由](02-longrun-routes.md)。
