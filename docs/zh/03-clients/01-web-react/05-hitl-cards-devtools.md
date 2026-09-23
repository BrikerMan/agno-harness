# 05. HITL / 卡片 / DevTools / 验收

协议细节回链，不在这里再堆一遍。

## 1. HITL

暂停那一轮已经 `RUN_FINISHED`。`isStreaming === false`。等人的是表单。

- `pendingTools.length > 0` 时禁用 Composer（见 [02](02-thread-shell.md)）。
- 答案 JSON、四种 pause、刷新还原 → [HITL 03 web-resume](../../02-interactions/02-hitl-and-actions/03-web-resume.md)。
- 线级协议 → [HITL 01](../../02-interactions/02-hitl-and-actions/01-protocol.md)。

跨页唤回见 [02 §跨页](02-thread-shell.md)。

## 2. 卡片与 Todo

- Class-First schema / `resolve` / `render_*` → [01 卡片](../../02-interactions/01-class-first-cards.md)。
- 前端 `data` vs `resolved`、Skill JIT → [07 Skills](../../02-interactions/07-skills-and-jit.md)。
- 长文档流式卡 → [08 Artifact](../../02-interactions/08-streaming-artifacts.md)。
- Todo 与 Chat 解耦 → [03 Todo](../../02-interactions/03-todo/README.md)。
- 委托文案：`delegate_subagent` 的 `description` 给 UI 标题（3–6 词），`prompt` 给子 agent；不要让模型再写一遍 fence。

## 3. DevTools

零改动接入与面板合同见 [第三层 01](../../04-deep-dive-and-faq/01-react-zero-code-integration.md)。

宿主约定：reducer 不得丢帧。Chunks 需要 `expose_debug_routes=True`；没有 debug 路由时省略 `fetchChunks`，页上显示「未开启」。

```tsx
<DebugPanel
  open={open}
  tab={tab}
  onTabChange={setTab}
  onClose={() => setOpen(false)}
  frames={chat.frames}
  violations={chat.violations}
  sharedState={chat.sharedState}
  statePatches={chat.statePatches}
  stats={chat.stats}
  lastRequest={chat.lastRequest}
  debugEnabled={debug}
  onClearFrames={chat.clearFrames}
  fetchChunks={() => fetch("/api/v1/debug/chunks").then((r) => r.json())}
  aguiPath="/api/v1/channels/web/agui"
/>
```

## 4. 验收清单

- 子 agent / 中途刷新 / HITL 确认框刷新仍在。
- long-run 开着时连点两次发送，确认没有两个 run。
- HITL 等待时输入框不能发新问题。
- `none` 下关 tab → run 停。`live` 下同样操作 → run 还在，下次能 attach。只有 Stop 才 abort。
- 长工具静默 >10s：SSE 仍在（可见 `: ping`）；掐代理后 idle → **attach**，任务不被 abort。
- `order[]` 里 reasoning / tool / subagent 在最终 text **之前**；上翻停止跟滚。
- 新对话侧栏是「新任务」，收到 `thread.title` 后同一行变成标题，不闪 UUID；徽章用 `runCount`。
- attach 续传：`currentId` 已挂回，后续 token 不会丢。
- [04 §F](04-attach-and-longrun.md) 全部勾上。
- Todo 侧栏六条：[Todo 02 §5](../../02-interactions/03-todo/02-sidebar-ui.md)。
- 长文档六条：[Artifact §5](../../02-interactions/08-streaming-artifacts.md)。
- 压缩卡六条：[压缩 03](../../02-interactions/04-compression-and-sealing/03-seal-and-faq.md)。
