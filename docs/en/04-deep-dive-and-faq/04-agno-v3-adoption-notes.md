# 04. Agno v3 adoption notes

Which Agno v3 features are worth adopting in agno-harness, and which are not. This page is a design note, not a to-do list: nothing here is implemented unless it says so. AgentOS features (durable queues, `user_isolation`, JWT, `GET /models`) are out of scope, because harness ships its own FastAPI and AG-UI layer.

What already landed in 0.3:

- Seal and checkpoint persist through `upsert_run` ([Seal](../02-interactions/04-compression-and-sealing/03-seal-and-faq.md)).
- `agno-harness db migrate-agno` for v2 databases ([Dual storage](../01-foundations/05-persistence-and-longruns/01-dual-storage.md)).
- `WorkspaceToolkit` refuses credential files by default.

## Worth building

### Guardrails (`agno.guardrails`)

`PIIDetectionGuardrail`, `PromptInjectionGuardrail` and `OpenAIModerationGuardrail` attach as `Agent(pre_hooks=[...])` and raise `InputCheckError`. Harness validates no input today; the chime-in policy only decides whether a group message is answered.

Sketch: a `Module` (or default pre-hook) that maps `InputCheckError` to an AG-UI `RUN_ERROR` frame with a safe message, and records the trigger in the audit sink. The built-in PII patterns are English-oriented; pass `custom_patterns` for Chinese mobile numbers and national ID numbers.

### Offloading large tool results (`Agent(offload_tool_results=True)`)

A tool result over the threshold is written to AgentFS; the transcript keeps a preview, the size, and a `result_id`. The model reads it back with `read_result` / `search_result`. It is lossless and costs no model call. It needs `SqliteDb` or `PostgresDb`.

It complements `SmartCompressionManager`: offloading shrinks the biggest messages first, so checkpoints trigger less often. Before adopting, check that the extra tool calls render sensibly in the AG-UI stream and that the `<prefix>_tool_results` table is covered by the table prefix (it is).

## Worth evaluating

- **`tool_call_limit`, `retries` / `exponential_backoff`, `tool_hooks`.** Harness has no run-level cap on tool calls and no run-level retry. A cap stops a looping agent; `tool_hooks` is the single place for tool timing, audit and redaction.
- **`@approval`.** `required` blocks until resolved; `audit` leaves a record. Harness HITL already pauses and resumes through `requirements`; the new part is the audit type, which could feed a sink.
- **`LearningMachine`.** User profile, user memory, session context, entity memory, learned knowledge and a decision log, in `ALWAYS`, `AGENTIC` or `PROPOSE` mode. Harness keeps no cross-session memory beyond the checkpoint. Entity memory in the `user` namespace was re-keyed in v3 (see the Agno migration guide), so migrate before enabling it.
- **`user_id` on every read.** Agno v3 filters `get_session` and `get_runs` by `user_id`. Audit `ThreadService` so every session and run read passes it.

## Leave as is

- **`SkillManager`.** It validates card declarations against the catalog and injects card schemas just in time; Agno's `Skills` does neither. Keep the `SKILL.md` frontmatter compatible so one file loads in both.
- **`WorkspaceToolkit`, `TodoToolkit`, sub-agents, compression, seal.** They are tied to the AG-UI stream and the card protocol; Agno's counterparts stop at the model layer.
- **`followups` and `enable_session_summaries`.** They could replace the hand-written titles and follow-ups, but the current ones are enough.

## Known cost to watch

`ThreadService.get_sessions` falls back to Agno sessions when the thread store is empty. On v3 each session attaches its runs from the runs table, because the sidebar needs a title and `runCount`. If that fallback ever gets hot, batch-read with `get_runs(session_id=..., limit=...)`.
