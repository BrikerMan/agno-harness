# 02. Thread shell

Titles, four-state sidebar, unread watermark, cross-page HITL recall, Composer. No extra HTTP routes.

## 1. Thread title: function + CUSTOM

Call a runtime function. The default post-run hook names the thread after the first successful run and yields `CUSTOM thread.title` on the same SSE (before `RUN_FINISHED`). To rename later, send `forwardedProps.refreshTitle = true`.

```python
title = await runtime.generate_thread_title(thread_id, user_id=..., max_turns=1)
runtime.on_post_run(make_thread_title_hook(runtime))
```

| Point | Contract |
| --- | --- |
| Auto-run | This thread has **no** saved title yet, and this run succeeded. Failed runs do not name. |
| Re-trigger | `forwardedProps.refreshTitle === true`. |
| Model input | First run: `scope.user_text` + completion. Later: last `max_turns` in the session. |
| Persist | Agno session `session_data["session_name"]`. |
| `GET /api/v1/threads` | `{ threadId, title, runCount, messageCount, updatedAt }`. `title` prefers the saved name, else first user message `[:40]`. `runCount` = how many times a human spoke. `messageCount` is **deprecated, always 0**. Sessions with no run stay off the list. |
| Event | `CUSTOM` `name="thread.title"` `value={ threadId, title }`. |
| Failure | Naming failure does not affect the main run. Sidebar stays “New task” or the user-message fallback. |
| LLM | Short completion on `agent.model`. **Do not** `agent.arun()`. |

The product must:

- Optimistic new row: **“New task”**. Never flash a UUID.
- **Create on send:** on the first message, register `{ threadId, title: "New task", runCount: 1, updatedAt }` locally.
- **Merge sidebar:** after `GET /api/v1/threads`, if the active `threadId` still has messages and is missing from the server list, pin it on top. The server takes over after the first run commits.
- Reducer: `thread.title` updates the current title; crossfade the same row, do not remount.

There is no `POST /api/v1/threads/{id}/title`. **Wrong:** sidebar `title || threadId`; a second HTTP call just to name.

Agno’s `AgentSession` commits only after the first run **fully finishes**. A refresh mid-first-run that trusts only `GET /api/v1/threads` will drop the current conversation.

## 2. Four-state sidebar

Agents are not chat. Users leave. Each thread is in exactly one state:

```
running ──HITL──▶ paused ──confirm──▶ idle
   │
   ▼
just_finished ──open / decay──▶ idle
```

| State | When | Visual | Intent |
| --- | --- | --- | --- |
| `running` | Streaming / tools / background | Blue dot or spinner | Safe to leave |
| `paused` | HITL | **Amber** | Highest-priority recall |
| `just_finished` | Just done, unread | Green check | Ready to review |
| `idle` | Read or expired | No badge | Quiet |

`runCount` badges are optional. Minimal products leave idle rows blank; support / ticket UIs may show a muted number.

### Three anti-spam rails

1. **Burn on open:** clicking a thread writes `lastViewedAt`; the check vanishes. No “mark as read” button.
2. **Time decay:** suggest `MAX_ALERT_AGE = 2 hours` (cap 4). Then `idle`.
3. **Cold-start silence:** new devices have no `lastViewedAt`; decay keeps the list clean.

### Do not poll blindly

No `setInterval(fetchThreads, 3000)`.

1. **Active thread:** live SSE / `/attach`. On `thread.title`, `run.paused`, or `RUN_FINISHED`, call `refreshThreads()` once.
2. **Window focus:** one fetch on `focus` / `visibilitychange` back to foreground.
3. **Adaptive poll:** only while local state knows a `running` / `paused` thread exists, every 10–15s. Stop when everything is terminal.

### Cross-page HITL recall

Do not keep agent state only inside the `/agent` route. Lift a store to the root layout:

- Navbar badge: amber for `paused`, blue for `running`, a count for pending decisions.
- Global toast on `paused`: “Needs confirmation” → “Handle now” routes to that thread.
- When the page is hidden: change `document.title`, optional favicon; `Notification` with `tag: hitl-${threadId}` to stay idempotent. Restore the title when visible.

### Pure status resolver

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

Store the view watermark in `localStorage` (e.g. `agui:thread_views`). Mark viewed on row click.

## 3. The rest of the shell

- The client generates `threadId`. Another user’s thread is `404`, never `403`. Switching login must `reset`.
- Identity follows the login session. Do not put `userId` in the `POST /api/v1/channels/web/agui` JSON.
- Switch thread: abort the **current stream** (the connection), `loadThread`, `stick.pin()`. Do **not** abort a long-run job just because you left.
- Delete the current thread, then `reset`.
- Composer: Enter sends, Shift+Enter newline. Disable new messages when **`isStreaming` or `pendingTools.length > 0`**. While streaming, the button is Stop.
- Stop means “do not run”: abort this HTTP when resume is `none`; after long-run also `POST /api/v1/runs/{id}/abort`. Closing a tab must **not** abort.
- `forwardedProps.reasoning` → thinking budget. If thinking is off, do not paint an empty reasoning bar.
- `dropEmptyTail`: drop an empty assistant bubble after abort.
- A mismatched `X-Agui-Protocol` must fail loudly, not half-render.

Next: [03 Stream and scroll](03-stream-and-scroll.md).
