# Changelog

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
