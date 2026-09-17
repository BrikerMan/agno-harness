# Demo 已知问题

对照 [`examples/demo/frontend/`](../examples/demo/frontend/) 代码记录。**本次不修。** `/cancel` → `/abort` 的接口改名已做，不等于修下面这些行为。

1. **Detach 连点发送会双开 run。** [`use-agui-chat.ts`](../examples/demo/frontend/src/hooks/use-agui-chat.ts) 每次 `newId("run")`，先 abort 连接再 `?detach=1`。不断开服务端任务，同一 thread 上两个模型。应先看 `/active`，有 running 则等或先 `POST /runs/{id}/abort`。
2. **侧栏闪 threadId。** [`ThreadList.tsx`](../examples/demo/frontend/src/components/chat/ThreadList.tsx) 用 `title || threadId`。没有「新任务」占位，也没听 `CUSTOM thread.title` 做 crossfade。
3. **回放时长会跳。** tool / sub-agent 的 `startedAt`/`endedAt` 用 `Date.now()`。frames 回放时应冻结，优先服务端 `elapsedMs`。
