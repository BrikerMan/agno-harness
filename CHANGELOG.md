# Changelog

## 0.3.10

### Fixed

- **Install `install_sealed_history_hook()` automatically in `AgentRuntime` and `make_checkpoint_hook`.** `AgentRuntime.__init__` and `make_checkpoint_hook` now install `_sealed_aware_get_messages` on `AgentSession.get_messages` automatically, so persisted context checkpoints (`metadata["checkpoint"]`) and sealed runs (`metadata["sealed"]`) prune prior session runs across turns without requiring legacy `compat.py` imports.
- **Skip inline `[Context Checkpoint]` messages when replaying checkpointed runs and persist checkpoints on error.** `reorganize_messages_with_checkpoint` marks synthetic checkpoint messages with `from_history=True`, `_sealed_aware_get_messages` skips leading `[Context Checkpoint]` messages in `cp_run.messages` so the checkpoint run's real user query is preserved, and `make_checkpoint_hook` saves checkpoints generated before mid-run failures via `_find_run_with_grace`.

### Changed

- Bump package version to `0.3.10`.

## 0.3.9

### Fixed

- **Surface `RUN_ERROR` instead of reporting "no additional text to display".** `MessageCollector` now records `EventType.RUN_ERROR` messages; when a model call times out or fails without producing text or cards, `RelayApp.handle_event` replies with an explicit error message and settles the turn reaction with `❌` instead of `✅`.
- **Ignore non-file `text/html` and card attachments in the fallback attachment summary.** Teams attaches a `text/html` body block to every inbound message; when `attachment_processor` returned empty because no document was attached, `RelayApp.handle_event` previously reported `• file (file is attached; content could not be read or permission required)`. Non-file `text/html`, card, and empty shell attachments are now skipped.
- **Prevent tool calls during in-context checkpoint compression.** `DEFAULT_IN_CONTEXT_CHECKPOINT_PROMPT` in `SmartCompressionManager` now explicitly forbids emitting tool calls during compaction, and `TeamsBotAdapter.clean_quote_text` strips inline markdown/HTML images and nested blockquotes from quoted reply headers.

### Changed

- Bump package version to `0.3.9`.

## 0.3.8

### Fixed

- **Encode multiline SSE frames with WHATWG-compliant `data:` prefixes.** Multiline payloads in SSE transport now prefix every line with `data: ` so clients do not drop lines after the first newline.

### Changed

- Bump package version to `0.3.8`.

## 0.3.7

### Fixed

- **Restrict `MentionOnlyPolicy` to the bot's own identity in group chats.** Group chat messages that mention another user or bot (or quote the bot's card without mentioning the bot in the active reply body) are now suppressed instead of triggering a reply.

### Changed

- Bump package version to `0.3.7`.

## 0.3.6

### Added

- **Modular channel packages (`channels/teams`, `channels/lark`, `channels/cli`, `channels/web`)** with clean backward-compatible re-exports.
- **Microsoft Graph attachment enrichment for Teams.** `TeamsGraphClient` fetches message attachments and downloads SharePoint/OneDrive shared files.

### Changed

- Bump package version to `0.3.6`.

## 0.3.5

### Changed

- **Background tasks use SQLite or Postgres.** `BackgroundTaskService` takes a filesystem path, a database URL, or an existing `AgnoHarnessDb`. The table is always `agno_harness_background_tasks`. It is not added to the app-prefixed harness tables (`moradin_threads` and the rest), and those tables are not modified.
- Bump package version to `0.3.5`.

## 0.3.4

### Changed

- **Resumable background tasks retry after an error, not only after a restart.** A failing `run` of a `resumable` handler runs again after `retry_delay × attempts` seconds (default 30) while attempts remain, and only the last failure is posted. Handlers can set `max_attempts` (default 2, counting the first run); `resume()` honours it too. `run` sees `task.attempts`, so a handler can pick up work it saved on an earlier attempt.
- Bump package version to `0.3.4`.

## 0.3.3

### Fixed

- **Chat cards still fell back to the title-only card.** `MessageCollector` read the card name from a `name` key, but `StreamUIModule` sends it as `schema`, so no block or item was ever found in the catalog. It now reads `schema`, and item lines that omit it take the block's `itemSchema`. Collector tests now feed events in the shape the parser emits.

### Changed

- **FINAL-mode replies drop narration.** In `StreamMode.FINAL`, text written before a tool call ("Let me check…") is discarded and only the text after the last tool call is sent; cards are kept. Streaming modes are unchanged. Opt out with `RelayApp(keep_interim_text=True)` or `MessageCollector(keep_interim_text=True)`.
- Bump package version to `0.3.3`.

## 0.3.2

### Fixed

- **Custom card renderers never ran for model-emitted cards.** The parser passes block props and item data with their `schema` routing key, which every schema model rejects as an extra field, so `render_teams_block` / `render_{platform}` were skipped silently and the generic title-only card was sent. The key is now dropped before validation, and a validation failure logs a warning before falling back.
- **Message audit rows failed for Teams personal chats.** Their conversation ids exceed 128 characters. `chat_id`, `thread_id` and `sender_id` on the message audit table are now `String(512)`; apps that manage harness tables with alembic need a revision (`ALTER COLUMN ... TYPE VARCHAR(512)`).

### Changed

- Bump package version to `0.3.2`.

## 0.3.1

### Added

- **Background task results can be cards.** A handler's `run` may return an `OutboundMessage`: its cards go to Teams and Lark, its text to other channels and into the task row. Plain `str` results behave as before.
- Optional handler hook `on_failure(task, error)` returns the message posted when a task fails or a restart interrupts it.
- **`BackgroundTaskService.resume()`**: call once at startup after `bind`. Unfinished tasks of a handler with `resumable = True` run again (at most 2 runs in total); other unfinished tasks are failed with "Interrupted by a restart." and their chat is told. The task row gains an `attempts` column, added in place on existing databases.
- **Teams turn reactions.** `ack` puts 🤔 (`think`) on the inbound message, and `settle` removes it and adds ✅ when the turn succeeds. Reaction failures are logged and never fail the turn. `TeamsConnectorClient` gains `put_reaction` / `delete_reaction`.

### Changed

- Bump package version to `0.3.1`.

## 0.3.0

Requires **Agno v3** (`agno>=3.0.10`). Agno v2 is no longer supported.

### Fixed

- **Seal and checkpoint were not persisted on Agno v3.** v3 stores each run in its own table and `upsert_session` no longer writes runs, so `seal_session_run` and `attach_checkpoint_to_session` returned `True` while the edited run was lost. Dangling tool calls stayed open and compression checkpoints never took effect. Both now write through `db.upsert_run(...)` (sync and async dbs); backends without `upsert_run` still fall back to `upsert_session`.
- `seal_session_run` waits briefly (up to about 0.5 s) for Agno to flush the interrupted run before falling back to the latest run, so it no longer races Agno's own cancel cleanup.

### Added

- **`agno-harness db migrate-agno`** (`--sqlite FILE` or `--url URL`, optional `--prefix` / `--session-table`) and `agno_harness.migrate_agno_db(db)`: run Agno's v2 to v3 migration and print a report. Non-destructive; never calls Agno's `cleanup_legacy_runs_*` and never passes `force=True`.
- `AgnoHarnessDb.from_agno_db(...)` logs a warning at startup when the paired Agno database still has the v2 schema.
- **`WorkspaceToolkit` refuses credential files by default**: `.env` (not `.env.example`), `*.pem`, `*.key`, `.ssh/`, `.aws/`, `credentials.json`, `*.tfvars` and similar are rejected for read and write, and left out of listings and search. Re-allow paths with `allow_paths=[...]`; turn the guard off with `block_credentials=False`.
- Docs: Agno v3 storage and the upgrade steps ([dual storage](docs/en/01-foundations/05-persistence-and-longruns/01-dual-storage.md)), and [Agno v3 adoption notes](docs/en/04-deep-dive-and-faq/04-agno-v3-adoption-notes.md).

### Changed

- Dependencies: `agno>=3.0.10`, `openinference-instrumentation-agno>=1.0.10`.
- **Upgrade:** back up the database, stop writers, then run `agno-harness db migrate-agno` before starting 0.3.
- Bump package version to `0.3.0`.

## 0.2.6

### Added

- **Per-call `agent_id` filter on the Python API**: `ThreadService` and `AgentRuntime` `list_threads` / `get_thread` / `delete_thread` (and `ThreadService.get_session` / `get_sessions`) take an optional `agent_id`. On an unscoped service it filters down to one agent's threads; on a scoped one it can only repeat the scope, anything else matches nothing. HTTP routes are unchanged.
- Subclasses that override `ThreadService.get_session` / `get_sessions` must accept the new `agent_id` keyword.

### Changed

- Bump package version to `0.2.6`.

## 0.2.5

### Added

- **Agent-scoped threads**: several runtimes can now share one set of harness tables without seeing each other's history.
  - `agno_harness_threads` gains a nullable, indexed `agent_id` column. **Requires a schema migration** (`ALTER TABLE ... ADD COLUMN agent_id VARCHAR(128)` + index); rows written earlier keep `NULL`.
  - `AgentRuntime(agent_id=...)` stamps every thread it starts and scopes its thread list, get, replay, frames and delete to that agent. Defaults to `agent.id` when the agent has one; with neither, the runtime stays unscoped as before.
  - `ThreadService(agent_id=...)` applies the same scope to the thread store and to Agno sessions (`component_id` filter plus a post-check). Deleting another agent's thread never reaches Agno's unscoped `delete_session`.
  - `BaseThreadStore.start_turn / get_thread / list_threads / delete_thread` accept an optional `agent_id`; thread summaries expose `agentId`.
  - A legacy thread with `agent_id = NULL` is adopted by the first agent that continues it and never reassigned. Scoped listings skip unstamped rows, so backfill them from the Agno session table: `UPDATE <prefix>_threads t SET agent_id = s.agent_id FROM <sessions> s WHERE s.session_id = t.thread_id AND t.agent_id IS NULL`.

### Changed

- Bump package version to `0.2.5`.

## 0.2.4

### Added

- **`KeywordKnowledge` + `CjkKeywordScorer`** (`agno_harness.knowledge`): Chinese keyword retrieval with no embeddings. Text is cut into character bigrams (Latin words stay whole) and ranked with BM25 over weighted fields. Each hit carries `score` and `matched_terms` (adjacent bigrams joined back into words). `KeywordKnowledge.search` plugs in as an Agno `knowledge_retriever`; with `enable_agentic_knowledge_filters` it validates the model's filters itself — unknown keys or values nobody has are dropped with a warning instead of returning nothing. Override `normalize_filter_value` to accept the model's wording (一般 → general). Documents come from a `load` callable and are cached until `invalidate()`.
- **Lark Card Action Callbacks & In-Place Updates**:
  - `LarkChannel` registers `card.action.trigger` event listener over WebSocket and translates clicks into `ChannelEvent(action_id)`.
  - Added support for in-place card updates via `extra={"update_in_place": True}` in `LarkChannel.send`.
- **Multimodal Inbound Handling & Attachment Downloads**:
  - `LarkChannel` extracts image resource keys from rich text post messages and downloads them via `fetch_attachment`.
  - `RelayApp` automatically downloads attachments before turn execution and passes images as AG-UI multimodal image content parts.
  - Query envelope preserves multimodal image parts instead of stringifying them.

### Changed

- `MarkdownKnowledge` ranks with the same bigram BM25, so Chinese notes match without spaces between words.
- Bump package version to `0.2.4`.

## 0.2.3

### Added

- **General-Purpose `WorkspaceToolkit`**:
  - Safe workspace inspection and exploration toolkit (`read`, `glob`, `grep`, `list_files`) shipped out-of-the-box in `agno_harness.tools`.
  - Configurable directory resolution: supports dynamic template variables (`workspaces/{user_id}/{thread_id}`), callable resolvers `(scope) -> Path`, and fallback to `RunScope.data["artifact_root_dir"]`.
  - Custom file resolution hook (`file_resolver: Callable[[Path, str], Path | None]`): allows upstream applications to resolve files across structured subdirectories with automatic sandbox escape checks.
  - Optional write tools (`allow_write: bool = False`): defaults to strictly read-only inspection. When enabled, registers `write_file` (with append support) and `patch_file` (exact Search & Replace).

### Changed

- **Purified `StreamingArtifactToolkit` (Zero Registered Tools by Default)**:
  - Removed default tool registrations (`read_artifact_section`, `patch_artifact`, `append_artifact`) so models are forced to stream `<stream-ui>` cards directly instead of blocking streams with JSON tool arguments.
  - Updated instruction prompt: explicitly emphasizes generating documents and presentations via `<stream-ui>` fences, resuming via `<stream-ui mode="append">`, and using workspace exploration tools for inspecting existing files.
- Bump package version to `0.2.3`.

## 0.2.2

### Added

- **Unified Artifact Lifecycle (write, append, patch)**:
  - `StreamUIModule._finish_block` natively supports three artifact persistence modes: full overwrite (`mode="write"`), incremental append (`mode="append"`), and exact diff patch (`mode="patch"`).
  - Added `persisted=True` block prop awareness: when an artifact file has already been saved to disk by a tool call, `StreamUIModule` avoids redundant re-writing and properly updates file metadata (`savedPath`, `relativePath`, `bytes`) while continuing the card event stream.
  - Added `BlockSchema.persists` and `CardCatalog.should_persist(name)`: allows non-persisted review schemas (such as `DiffCard`) to bypass file saving, avoiding destructive file overrides.
  - Added `DiffCard` (`schema_name="diff"`, `persists=False`) to standard artifact schemas.
  - Added `BlockSchema.before_save` and `after_save` lifecycle hooks, ensuring downstream post-processing (such as document compilers or slide-to-pptx converters) execute strictly after the physical file has safely landed on disk.
  - Linked post-patch re-transcoding: when a `diff` block provides `target_schema`, `StreamUIModule` triggers `after_save` and `on_complete` hooks of the target schema on the modified file.

### Fixed

- **Artifact Tool & StreamUI Lifecycle Disconnect**:
  - `append_artifact` now invokes `ui_block(schema, mode="append", persisted=True)` upon appending to disk, emitting real-time slide/item progress and triggering downstream compilers.
  - `patch_artifact` safely replaces targeted text chunks without corrupting the file with raw diff fences, and emits a standard `DiffCard` stream for UI inspection.
  - Prevented `StreamUIModule` from writing raw git diff hunks directly over target source files when `ui_block("diff")` finishes.

### Changed

- Bump package version to `0.2.2`.

## 0.2.1

### Fixed

- **LongRunManager Thread Registration (P0)**: Auto-generate `thread_id` at the start of `start()` when none is provided, avoiding registration under the empty string key `runs:thread:` and ensuring the first turn is immediately queryable via `/frames` and `/active`.
- **HITL Pause Event Duplication (P1)**: Suppress duplicate `TEXT_MESSAGE_*` events and re-emitted `TOOL_CALL_START` / `TOOL_CALL_ARGS` events during `RunPausedEvent` completion when chunks have already been streamed live, preventing duplicated assistant speech and tool payload echoes.
- **SSE Stream Protocol & Watermark (P1)**: Include standard SSE `id: {now_ms}-{seq}` watermarks on all live stream frames in `make_agui_router`. Default to long-run detachment mode when `long_runs` is configured, and respect `Last-Event-ID` / `?after=` to resume without replaying entire turn history.
- **Resume Turn User Input Echo (P2)**: Extract actual user choices and answers from trailing `ToolMessage` payloads via `extract_resume_input` instead of echoing the initial turn's question in `RUN_STARTED` events.
- **SubAgent Resilience & Timeout**: Added `first_chunk_timeout` (75s default), `inter_chunk_timeout` (120s), and automatic retry (`max_attempts=2`) to `SubAgentToolkit.delegate_subagent`, mitigating upstream model silence and hang issues with detailed error context on exhaustion.
- **Thread Store Turn Alignment**: Fixed `ThreadStore.start_turn` call to pass `scope.user_text` rather than a `RunAgentInput` object.
- **Database Engine Ownership**: Added `owns_engine` parameter to `AgnoHarnessPostgresDb` and `SqliteDb` subclasses to control external engine lifecycle management.
- **Session Fallback**: Safely handle store exceptions in `list_threads` and fall back to `get_sessions` when thread store is uninitialized or empty.

### Changed

- Bump package version to `0.2.1`.

## 0.2.0

### Added

- `AgnoHarnessSqliteDb`, `AgnoHarnessPostgresDb`, and `AlembicMigrator` for clean separation between Agno conversational context and Harness UI event stream / card persistence, supporting both silent local auto-initialization and enterprise Alembic migrations.
- Dynamic table prefix support with both hyphen (`-`) and underscore (`_`) styles, adjusting table names appropriately (e.g. `admin_agent_conversation_sessions`) to enable clean multi-agent database cohabitation.
- Automatic wiring of persistence stores, `SQLAlchemyActionStore`, `SQLAlchemySessionStore`, and `SQLAlchemySink` in `AgentRuntime` and `RelayApp` when `harness_db` is provided.
- HTTP routes now live under `/api/v1`. The web agent is `/api/v1/channels/web/agui`, and the Teams bot is `/api/v1/channels/teams/messages`. Health, threads, runs, and debug use the same prefix.
- `resources/templates` is the project `agno-harness init` copies: a coordinator, a researcher helper, cards, Markdown notes, and channel entrypoints.
- Teams onboarding goes through the Developer CLI. It prints an install URL, checks the bot with `teams doctor`, verifies the incoming JWT, and sends replies through the Bot Connector.
- Background tasks keep one SQLite row, run in the process, and reply in the chat that started them.
- `ExaTools` searches the web through the public Exa MCP endpoint. No API key.
- Markdown notes in the project can be searched as knowledge.

### Fixed

- The storage guard no longer treats an in-memory log stand-in as a durable SQL store. That includes `InMemory` stores, `HistoryOnly`, `FakeLog`, `BrokenLog`, and a wrapper around one of those.
- A long run keeps its own heartbeat task, so a quiet model stream still refreshes the run.
- The Redis run heartbeat lasts 300 seconds. If the process stops answering, the run is stored as an error, and frames written as it stops are still returned.
- A model stream that stays silent for 300 seconds fails with a timeout.
- Starting a run closes an older paused run on the same thread.
- An open Stream UI block is closed as truncated when the run ends, and that truncated flag stays on the block.

### Changed

- PostgreSQL stores default to native `JSONB` on PostgreSQL dialects with automatic `JSON` fallback on SQLite.
- The docs, the React frontend kit, and the project template use the `/api/v1` channel paths.
- Format and lint include `resources/templates`.

## 0.1.0

Initial release. One Runtime and one Relay run the same agent in the terminal, the browser, Teams, and Lark, with chat history, cards, compression, human approval, and resume.
