# 05. HITL / cards / DevTools / checklist

Protocol details live on their own pages. This step only closes the loop.

## 1. HITL

The paused turn already `RUN_FINISHED`. `isStreaming === false`. The wait is a form.

- Disable Composer while `pendingTools.length > 0` (see [02](02-thread-shell.md)).
- Answer JSON, the four pauses, refresh restore → [HITL 03 web-resume](../../02-interactions/02-hitl-and-actions/03-web-resume.md).
- Wire protocol → [HITL 01](../../02-interactions/02-hitl-and-actions/01-protocol.md).

Cross-page recall: [02](02-thread-shell.md).

## 2. Cards and todos

- Class-First schema / `resolve` / `render_*` → [01 cards](../../02-interactions/01-class-first-cards.md).
- Frontend `data` vs `resolved`, Skill JIT → [07 Skills](../../02-interactions/07-skills-and-jit.md).
- Long-document streaming cards → [08 Artifacts](../../02-interactions/08-streaming-artifacts.md).
- Todos decoupled from chat → [03 Todos](../../02-interactions/03-todo/README.md).
- Delegation copy: `delegate_subagent` `description` is the UI title (3–6 words); `prompt` goes to the child. Do not let the model rewrite that card as a fence.

## 3. DevTools

Zero-change integration and the panel contract: [layer 3 / 01](../../04-deep-dive-and-faq/01-react-zero-code-integration.md).

Host rule: the reducer must not drop frames. Chunks need `expose_debug_routes=True`. If debug routes are off, omit `fetchChunks` and show “not enabled”.

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

## 4. Acceptance checklist

- Sub-agent / mid-run reload / HITL confirm still work after refresh.
- With long-run on, a double send is still one run.
- Composer cannot send while HITL is waiting.
- Tab close on `none` stops the run. The same on `live` leaves the run up; next open can attach. Only Stop aborts.
- A silent tool >10s: SSE still alive (`: ping`); after a proxy cut, idle → **attach**, the job is not aborted.
- In `order[]`, reasoning / tool / subagent appear **before** final text. Scroll-up stops follow.
- A new chat sidebar says “New task”; `thread.title` crossfades the same row, no UUID flash; badge uses `runCount`.
- After attach, `currentId` is restored and later tokens are not dropped.
- [04 §F](04-attach-and-longrun.md) is fully checked.
- Todo sidebar six rows: [Todo 02 §5](../../02-interactions/03-todo/02-sidebar-ui.md).
- Long-doc six rows: [Artifact §5](../../02-interactions/08-streaming-artifacts.md).
- Compression-card six rows: [compression 03](../../02-interactions/04-compression-and-sealing/03-seal-and-faq.md).
