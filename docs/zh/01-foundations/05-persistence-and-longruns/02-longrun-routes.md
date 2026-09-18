# 02. 长任务路由

```python
from agno_harness import LongRunManager, make_agui_router

long_runs = LongRunManager(runtime, log=stores.event_log, stream=stores.event_stream)
app.include_router(make_agui_router(runtime, long_runs=long_runs, resolve_user_id=...))
# lifespan exit:
await long_runs.shutdown()
```

Relay 路径用 `relay.get_router(...)`，同样把 `long_runs` 编进去。没挂就是 `/runs/*` **404**。

| Route | 作用 |
| --- | --- |
| `POST /agui?long-run=1` | 后台跑；关 tab 不停（亦 `?long_run=1`、旧 `?detach=1`） |
| `GET /runs/{id}/attach?after=` | **canonical** 挂回正在输出的 run |
| `GET /runs/{id}/stream?after=` | deprecated，与 `/attach` 等价 |
| `GET /threads/{id}/active` | 仍在跑 **或 HITL paused**（`is_open`），含 `input`。paused 不再写帧（`is_producing` 为假），attach/tail 吐完已存的就结束 |
| `POST /runs/{id}/abort` | 用户点 Stop。响应 `{ aborted: bool }` |

关 tab / 闪退 ≠ abort。同一 thread 同时一个 open run。同一 `runId` 的 `start` 幂等；客户端每次 `newId("run")` 不会停掉已 long-run 的任务。

| 层 | 机制 | 作用 |
| --- | --- | --- |
| Wire | SSE comment `: ping`（第 0s 首发，约每 5s） | 防 Nginx/ALB 掐 TCP；不进 reducer |
| Worker | Redis beat TTL | 进程是否还在写 log |
| Resume | `GET /runs/{id}/attach?after=` | 断线/刷新按游标续写 |

前端：任意字节（含 ping）重置 idle；约 3× 心跳静默则再 attach。实现细节只在 [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md)。

下一步：[03 FAQ](03-faq.md)。
