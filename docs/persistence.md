# 持久化与长任务

何时读：刷新还要看到卡片/子面板，或 run 必须活过那条 HTTP。前端逐步实现（localStorage、idle re-attach、自检清单）见 [frontend-ui.md](frontend-ui.md) §4.2；本篇偏存储与路由。

Agno session 是给**下一轮模型**的，不是用户看过的流。

两套存储：

- **热 log**（Redis，或进程内 stand-in）— 每个 AG-UI delta、可阻塞 tail、TTL。`?long-run=1`（兼容 `?detach=1`）和 `GET /runs/{id}/attach`（旧名 `/stream` 已 deprecated）读它。不要用 SQL 装 token。
- **History archive**（SQL）— 结束后合成的事件列表。连续 content delta 折成一帧。`GET /threads/{id}/frames` 走这里。

Toolbox 不建表。用 mixin 拼到你的 `Base`，自己 `create_all` / Alembic。

| 配了什么 | `X-Agui-Resume` | 行为 |
| --- | --- | --- |
| 没有 | `none` | 连接掉 = run 停 |
| 只有 `RunEventLog` | `history` | 后台跑完；不能句中 follow |
| 再加上 `RunEventStream` | `live` | `after=` 接到句中 |

跨进程 live 需要真 Redis。`memory://` 只服务本进程。写 log 失败标 `unrecordable`，不杀流。

## Long run

```python
from agno_relay.runtime.longrun import LongRunManager

long_runs = LongRunManager(runtime, log=stores.event_log, stream=stores.event_stream)
app.include_router(make_agui_router(runtime, long_runs=long_runs, resolve_user_id=...))
# lifespan exit:
await long_runs.shutdown()
```

| Route | 作用 |
| --- | --- |
| `POST /agui?long-run=1` | 后台长任务跑；关 tab 不停（亦支持 `?long_run=1` 与旧参数 `?detach=1`） |
| `GET /runs/{id}/attach?after=` | **canonical**：挂回正在输出的 run（SSE） |
| `GET /runs/{id}/stream?after=` | **deprecated**，与 `/attach` 等价，仅兼容旧客户端 |
| `GET /threads/{id}/active` | 仍在跑 **或 HITL paused**（`is_open`），含 `input`（用户 prompt）。paused 不再写帧（`is_producing` 为假），attach/tail 吐完已存的就结束 |
| `POST /runs/{id}/abort` | 用户点 Stop。响应 `{ aborted: bool }` |

关 tab / 闪退 ≠ abort。同一 thread 同时一个 open run。同一 `runId` 的 `start` 幂等；客户端每次 `newId("run")` 不会停掉已 long-run 的任务。

两种 cursor：SSE `id:` = 单 run log offset（给 **attach**）；frames id = `{runId}:{paddedOffset}`（只给 `/frames` 分页）。

### Wire keepalive vs Redis heartbeat vs attach

| 层 | 机制 | 作用 |
| --- | --- | --- |
| Wire | SSE comment `: ping`（第 0s 首发，每约 5s 定时） | 防 Nginx/ALB 静默掐 TCP，冲刷代理缓冲；不进 AG-UI reducer |
| Worker | Redis beat TTL | 进程是否还在写 log |
| Resume | `GET /runs/{id}/attach?after=` | 断线/刷新后按游标续写 |

前端：任意字节（含 ping）重置 idle；约 3× 心跳静默则 abort 并再 attach。

---

## 常见坑点与架构解析 (FAQ & Pitfalls)

### Q1: 长任务运行中刷新页面，为什么用户的提问会凭空消失？
* **根因**：Agno 原生设计中，`AgentSession` 是为**下一轮模型推理上下文**服务的。它是一个事务性的 Post-run commit 操作——只有在整个 `agent.arun()` 全部执行完毕后，Agno 才会把本轮的提问和回答写入 SQL session 数据库。如果在执行途中刷新，去查 Agno 的 `GET /threads/{id}/messages`，里面根本还没有这一轮的用户提问！
* **方案 A（协议级推荐：Frames 包含 User Message）**：
  * **信源单一化**：UI 渲染以 **Frames（展示帧）** 为唯一真实信源，不再依赖尚未落盘的 Agno Session `/messages`。
  * **协议保证**：
    1. 服务端发出的首帧 `RUN_STARTED` 事件在 `rawEvent` 中携带用户的最新 Prompt：`rawEvent: { protocol: "1.0", user_input: "...", input: "..." }`；
    2. `LongRunManager.start` 立即在 `RunRecord.input` 中保存该 Prompt；
    3. 前端 reducer 在收到 `RUN_STARTED`（无论是 live 还是 replay）时，若当前 transcript 尚未包含此 prompt，自动前置渲染对应的用户提问气泡。
  * **效果**：回放时直接通过 `GET /threads/{id}/frames` 就能 100% 完整还原“用户问题 + 推理思考 + 工具调用 + 增量回答”，哪怕 Agno 数据库里还没落盘。

### Q2: 为什么长任务刷新后，Stream 就断了、不再持续输出？
* **根因**：普通 HTTP 请求强依赖 TCP 长连接。浏览器刷新会直接发 RST 关闭连接，操作系统清理 socket。如果后端没有将任务生命周期与 HTTP 连接脱耦，协程随之退出，流自然永久中断。
* **解法**：
  1. 发送请求时带上参数：`POST /agui?long-run=1`（或 `?long_run=1`）；
  2. 后端检测到 `is_long_run`，由 `LongRunManager` 将 Agent 派发到独立的后台 `asyncio.Task` 托管运行，流式结果持续写入 Redis 热日志；
  3. 刷新后前端执行 **重连时序（Attach）**：
     * 先调用 `GET /threads/{id}/active`，发现任务仍在 `running`；
     * 调 `GET /runs/{id}/attach?after={lastOffset}` 连接到流尾部，无缝继续打字。

### Q3: 为什么正在处理的消息在 UI 上一片空白？
* **根因**：前端页面重新载入时，如果只拉取了已结束归档的数据库记录，而没有拉取热日志（Redis / 内存），或者没有先查 `/active`，前端就不知道当前有一个 run 正在生成。
* **标准重连时序**：
  ```
  页面加载 (Reload)
     │
     ├── 1. GET /threads/{id}/frames ──────> 还原已有全部帧 (包括进行中 run 已吐出的文字与工具)
     │
     ├── 2. GET /threads/{id}/active ──────> 获取 running 的 runId 及 input
     │         │
     │         ├─ 若 active.input 尚未显示 ──> 立即置顶展示用户提问
     │         └─ 将状态置为 isStreaming = true, currentId = "assistant-{runId}"
     │
     └── 3. GET /runs/{runId}/attach?after=lastOffset ──> 挂载 SSE 续写流
  ```
  按照此时序，用户刷新后 0 延迟看到自己刚才发的问题、Agent 已经写了一半的回答，并紧接着看到光标继续往后打字。

### Q4: 用户主动取消（Stop）任务时的时序与事件保证是什么？
* **根因与痛点**：用户点击 Stop 时，客户端会切断当前的 SSE 连接，并调用 `POST /runs/{id}/abort`。如果服务端仅仅调用 `task.cancel()` 杀掉协程，而不向日志落帧，后续重连或通过 `/threads/{id}/frames` 回放时，这条消息既没有终态也没有取消标识，UI 往往只能误当成“正常完成”，甚至显示“已完成工作 用时 X 秒”。若直接将其作为崩溃转成 `RUN_ERROR`，前端又会弹出刺眼的红色错误卡片。
* **解法（CUSTOM: run.cancelled 事件保障）**：
  1. 客户端点击 Stop 触发 `POST /runs/{id}/abort`，服务端 `LongRunManager` 标记 `RunStatus.ABORTED`；
  2. 在后台协程被取消时，通过 `asyncio.shield` 向热日志中追加 `CUSTOM` 事件：
     ```json
     {
       "type": "CUSTOM",
       "name": "run.cancelled",
       "value": { "reason": "user_aborted" }
     }
     ```
  3. 该事件随之落盘到持久化归档（`history_archive`），并向当时的本地 follower 广播；
  4. 前端无论是实时监听还是回放 `/threads/{id}/frames`，收到 `run.cancelled` 时即可将该消息置为 `cancelled` 状态，呈现“已停止生成”轻量中性标签，并将未闭合的工具和步骤优雅收口。

### Q5: 为什么新建会话在首轮运行中刷新，`GET /threads` 侧栏里找不到？
* **根因**：Agno 设计中，Session 是整个 `agent.arun()` 跑完才落盘到 SQL 数据库。在首个 run 产生并完成前，数据库中压根没有这个 session 记录，`GET /threads` 自然不包含它（无 run 的空 session 也按规范不进列表）。
* **解法（前端乐观缓存）**：用户发问时前端立即在本地缓存该 active thread（`{ threadId, title: "新任务", runCount: 1 }`）。拉取 `GET /threads` 时自动合并本地尚未落盘的会话。直到首轮跑完落盘后，服务端数据自然无缝接管（见 `frontend-ui.md` §1.1 & §4.2 E）。
