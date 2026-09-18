# 03. Web: forms, refresh, answers on the card

Shell iron rules stay on [Web 01](../../03-clients/01-web-react/01-iron-rules.md). This page is how a paused card comes back.

## Interaction

- Anything in `pendingTools`: paint the form and **disable Composer and scene switches**. The stream already ended; gating only on `isStreaming` lets the user send a new turn that wipes the confirm.
- confirmation: Allow / Deny → `{accepted}` (optional `note`).
- user input: from `userInputSchema` → `{values}` (keys are field names).
- user feedback: choices → `{selections}` (key is the question text; value is a label array).
- frontend tool: run `external_execution` automatically (`useRef` against StrictMode double-fire); send the result as a tool message.
- Patch the same `toolCallId`. Resume will replay `TOOL_CALL_*`.
- `stampToolAnswers` on submit so the card shows the answer immediately, without waiting for the next turn’s frames.

```ts
await send({
  toolResults: [{
    toolCallId: pendingTool.toolCallId,
    content: JSON.stringify({ accepted: true }),
  }],
});
```

## Refresh

- A paused run is on `/active` and needs no heartbeat. Restore `pendingTools` from frames. **Do not** live-tail a paused run that already finished producing.
- A historical `run.paused` must dismiss the form after a later `RUN_STARTED` / `TOOL_CALL_RESULT`. Dismiss also when `/active` has no paused run.
- Prefer `RUN_STARTED.user_input` on frames for the prompt. HITL resume rewrites the same sentence — dedupe consecutive user bubbles.

**Wrong:** refresh that only trusts whether `/active` still has a schema; stuffing the answer into a new user message.

## How the card shows the answer

| Source | Shown as |
| --- | --- |
| `accepted` | **DECISION** Allowed / Denied |
| user_input `values` | Field names (RECIPIENT / MESSAGE) |
| user_feedback `selections` | Question text → chosen labels |
| frontend tool | ERROR / result fields |

Live: `stampToolAnswers` object-merges into `args` so a later resume `{path}` does not wipe `accepted`.

Replay: prefer `TOOL_CALL_RESULT` (the runtime writes the trailing ToolMessage under the same `toolCallId`). Do not infer Deny from session `status: error` plus an empty result — that is an Agno side effect, not the protocol.

Cross-page banners and desktop notifications: [Web 02](../../03-clients/01-web-react/02-thread-shell.md).
