# 03. 持久化 FAQ

### Q0: 为什么要两套库？

Agno `db` 给下一轮模型。Harness `Stores` 给用户看过的流。只有 frames、没有 Agno `db`：UI 像完整，模型失忆。只有 Agno `db`、没有 Stores：提示词记得，刷新丢卡片。Harness 已是持久 SQL、Agno `db` 缺失或内存时，`AgentRuntime` 拒绝启动，除非 `allow_ephemeral_agno_db=True`。


### Q1: 长任务刷新后，用户提问凭空消失？

Agno 的 `AgentSession` 是 **post-run 事务**：整轮 `arun()` 结束才把本轮问答写入 SQL。中途去 `GET /threads/{id}/messages`，这一轮的用户句还不在。

方案：UI 只信 **Frames**。

1. 首帧 `RUN_STARTED` 的 `rawEvent` 带 `user_input` / `input`；
2. `LongRunManager.start` 立刻把 Prompt 写入 `RunRecord.input`；
3. reducer 收到 `RUN_STARTED` 时，若 transcript 还没有这句，前置用户气泡。

回放单靠 `GET /threads/{id}/frames` 就能还原问句 + 思考 + 工具 + 增量回答。

### Q2: 刷新后 Stream 断了、不再输出？

普通 HTTP 跟 TCP 绑死。刷新 RST 掉连接，若 run 没和连接脱钩，协程退出。

发送带 `POST /agui?long-run=1`；`LongRunManager` 把 Agent 放到独立 `asyncio.Task`，帧写入热 log。回来：`GET /threads/{id}/active` 仍 `running` → `GET /runs/{id}/attach?after=`。前端时序见 [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md)。

### Q3: 正在处理的消息一片空白？

只拉了 archive、没查 `/active`，前端不知道有 run 在写。

```text
Reload
  → GET /threads/{id}/frames     已吐出的字和工具
  → GET /threads/{id}/active     runId + input；缺问句就置顶；isStreaming = true
  → GET /runs/{runId}/attach?after=  续写
```

### Q4: Stop 之后回放像「已完成」或红错误？

只 `task.cancel()` 不落帧，回放没有终态。abort 后热日志追加：

```json
{ "type": "CUSTOM", "name": "run.cancelled", "value": { "reason": "user_aborted" } }
```

前端画「已停止生成」，收口未闭合工具，不要 `RUN_ERROR` 红卡，也不要标完成。

### Q5: 首轮进行中刷新，`GET /threads` 侧栏找不到？

Session 要等首个 run 跑完才落盘。发问时本地登记 `{ threadId, title: "新任务", runCount: 1 }`，拉列表时合并。见 [Web 02](../../03-clients/01-web-react/02-thread-shell.md)。
