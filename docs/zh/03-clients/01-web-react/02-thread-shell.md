# 02. Thread 外壳

标题、侧栏四态、未读水位、跨页 HITL 唤回、Composer。不新增 HTTP 路由。

## 1. Thread 标题：函数 + CUSTOM

产品调 runtime 函数。默认用 post-run hook 在首轮成功结束后起名，并往同一条 SSE 里 yield `CUSTOM thread.title`（落在 `RUN_FINISHED` 之前）。第二轮再起名：这次 `send` 带 `forwardedProps.refreshTitle = true`。

```python
title = await runtime.generate_thread_title(thread_id, user_id=..., max_turns=1)
runtime.on_post_run(make_thread_title_hook(runtime))
```

| 点 | 约定 |
| --- | --- |
| 何时自动跑 | 本 thread **还没有**已保存标题，且本 run 成功。失败的 run 不起名。 |
| 再触发 | `forwardedProps.refreshTitle === true` 时强制再生成。 |
| 模型输入 | 第一轮：`scope.user_text` + completion。再触发：session 最近 `max_turns` 轮。 |
| 落盘 | Agno session `session_data["session_name"]`。 |
| `GET /api/v1/threads` | `{ threadId, title, runCount, messageCount, updatedAt }`。`title` 先读已保存标题，没有才回退「首条用户消息[:40]」。`runCount` = 人说了几句。`messageCount` **已废弃，恒为 0**。无 run 的 session 不进列表。 |
| 事件 | `CUSTOM` `name="thread.title"` `value={ threadId, title }`。 |
| 失败 | 不起名不影响主 run；侧栏保持「新任务」或用户消息回退。 |
| LLM | 走 `agent.model` 短补全，**不要** `agent.arun()`。 |

前端必须做：

- 新对话乐观行：**「新任务」**，禁止闪 UUID。
- **发问即建档**：第一条消息发出时立刻在本地登记 `{ threadId, title: "新任务", runCount: 1, updatedAt }`。
- **侧栏合并**：`GET /api/v1/threads` 之后，若当前 `threadId` 仍有消息且不在服务端列表，置顶合并。首轮结束后服务端接管。
- reducer：`thread.title` 更新当前标题；同一行 crossfade，不要 remount。

没有 `POST /api/v1/threads/{id}/title`。**错法：** 侧栏 `title || threadId`；为起名再打一条 HTTP。

Agno 的 `AgentSession` 只有首个 run **完整结束**后才写入数据库。首轮进行中刷新，纯依赖 `GET /api/v1/threads` 会让当前会话凭空消失。

## 2. 四态侧栏

Agent 不是即时聊天：用户常切走。侧栏每个 Thread 只处在四种状态之一：

```
running ──HITL──▶ paused ──确认──▶ idle
   │
   ▼
just_finished ──点进 / 时效衰减──▶ idle
```

| 状态 | 触发 | 视觉 | 心智 |
| --- | --- | --- | --- |
| `running` | 流式 / 工具 / 后台 | 蓝点或 spinner | 可以离开 |
| `paused` | HITL | **琥珀色** | 最高优先级唤回 |
| `just_finished` | 刚完且未读 | 绿勾 | 成果就绪 |
| `idle` | 已读或过期 | 无徽章 | 清爽 |

`runCount` 徽章可选。极简产品 idle 时右侧留白；客服 / 工单才显示浅灰数字。

### 防骚扰三道护栏

1. **点击即焚：** 点进 Thread 立刻写 `lastViewedAt`，绿勾消失。不要「标记已读」按钮。
2. **时效衰减：** 建议 `MAX_ALERT_AGE = 2 小时`（上限 4 小时），过期自动 `idle`。
3. **冷启动静默：** 新设备没有 `lastViewedAt` 时，靠时效衰减，避免满屏绿勾。

### 不要无脑轮询

禁止 `setInterval(fetchThreads, 3000)`。

1. **当前会话：** live SSE / `/attach`。收到 `thread.title`、`run.paused`、`RUN_FINISHED` 时单次 `refreshThreads()`。
2. **窗口聚焦：** `focus` / `visibilitychange` 回前台时拉一次。
3. **伴随短轮询：** 仅当本地知道有 `running` / `paused` 时 10–15s 拉一次；全部终态后停掉。

### 跨页 HITL 唤回

不要把 Agent 状态只放在 `/agent` 路由里。全站 Root Layout 挂全局 Store：

- 顶栏角标：`paused` 琥珀色、`running` 蓝、待决策数字。
- 全局 Toast：`paused` 时滑出「需要确认」，「立即处理」跳到该 thread。
- 页面隐藏时：改 `document.title`、可选 favicon；`Notification` 用 `tag: hitl-${threadId}` 防重复。切回后恢复标题。

### 状态判定纯函数

```ts
export type ThreadDisplayStatus = "running" | "paused" | "just_finished" | "idle";
const MAX_ALERT_AGE_MS = 2 * 60 * 60 * 1000;

export function resolveThreadStatus({
  thread, lastViewedAt, isActive, activeState,
}: {
  thread: { threadId: string; updatedAt?: number | null; status?: string };
  lastViewedAt?: number;
  isActive: boolean;
  activeState?: { isStreaming: boolean; hasPendingHitl: boolean };
}): ThreadDisplayStatus {
  if (isActive && activeState) {
    if (activeState.hasPendingHitl) return "paused";
    if (activeState.isStreaming) return "running";
    return "idle";
  }
  if (thread.status === "paused") return "paused";
  if (thread.status === "running") return "running";
  const updatedEpochMs = thread.updatedAt ? thread.updatedAt * 1000 : 0;
  if (!updatedEpochMs || Date.now() - updatedEpochMs > MAX_ALERT_AGE_MS) return "idle";
  if (isActive) return "idle";
  if (!lastViewedAt || updatedEpochMs > lastViewedAt) return "just_finished";
  return "idle";
}
```

阅读水位放 `localStorage`（如 `agui:thread_views`）。点击行先 `markThreadViewed`。

## 3. 其余外壳

- `threadId` 前端自己生成。读到别人的 thread 回 `404`，不要 `403`。换登录用户必须 `reset`。
- 身份跟登录走（cookie / session），不要把 `userId` 放进 `POST /api/v1/channels/web/agui` 的 JSON。
- 切 thread：abort **当前流**（连接）、`loadThread`、`stick.pin()`。切走 **不要** abort 已 long-run 的任务。
- 删当前 thread：删完 `reset`。
- Composer：Enter 发送 / Shift+Enter 换行；**`isStreaming` 或 `pendingTools.length > 0` 都要禁用新消息**。streaming 时按钮变 Stop。
- Stop 才是「不要跑了」：`none` 下 abort 这条 HTTP；`long-run` 后还要 `POST /api/v1/runs/{id}/abort`。关 tab **禁止**因此 abort。
- `forwardedProps.reasoning` → thinking budget；关 thinking 就不要画空 reasoning 条。
- `dropEmptyTail`：中止后丢掉空 assistant 气泡。
- `X-Agui-Protocol` 对不上要明确报，不要半渲染。

下一步：[03 事件流与跟滚](03-stream-and-scroll.md)。
