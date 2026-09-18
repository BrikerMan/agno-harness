# 03. Persistence FAQ

### Q1: After a refresh mid long-run, the user prompt vanished?

Agno’s `AgentSession` is a **post-run transaction**: this turn’s Q&A hits SQL only after `arun()` finishes. Mid-run `GET /threads/{id}/messages` does not contain this prompt.

Trust **Frames** only.

1. The first `RUN_STARTED` carries `user_input` / `input` on `rawEvent`;
2. `LongRunManager.start` writes the prompt into `RunRecord.input` immediately;
3. On `RUN_STARTED`, if the transcript has no matching user bubble, prepend one.

Replay from `GET /threads/{id}/frames` alone restores prompt + reasoning + tools + incremental text.

### Q2: After refresh the stream dies and never continues?

Plain HTTP dies with TCP. A refresh RSTs the socket; if the run is tied to that connection, the coroutine exits.

Send `POST /agui?long-run=1`. `LongRunManager` hosts the agent on its own `asyncio.Task` and writes frames to the hot log. On return: `GET /threads/{id}/active` still `running` → `GET /runs/{id}/attach?after=`. Client sequence: [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md).

### Q3: The in-flight message is a blank bubble?

You loaded the archive and skipped `/active`, so the client does not know a run is producing.

```text
Reload
  → GET /threads/{id}/frames     already-emitted text and tools
  → GET /threads/{id}/active     runId + input; prepend the prompt if missing; isStreaming = true
  → GET /runs/{runId}/attach?after=  continue
```

### Q4: After Stop, replay looks “completed” or shows a red error?

`task.cancel()` with no frame leaves replay without a terminal. After abort, append to the hot log:

```json
{ "type": "CUSTOM", "name": "run.cancelled", "value": { "reason": "user_aborted" } }
```

Paint “generation stopped”, close open tools. Not a `RUN_ERROR` card, not “completed”.

### Q5: Mid first run, `GET /threads` does not list this session?

The session commits only after the first run finishes. On send, register `{ threadId, title: "New task", runCount: 1 }` locally and merge on fetch. See [Web 02](../../03-clients/01-web-react/02-thread-shell.md).
