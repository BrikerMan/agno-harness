# Changelog

## 0.2.0

### Added

- `AgnoHarnessSqliteDb`, `AgnoHarnessPostgresDb`, and `AlembicMigrator` for clean separation between Agno conversational context and Harness UI event stream / card persistence, supporting both silent local auto-initialization and enterprise Alembic migrations.
- Dynamic table prefix support with both hyphen (`-`) and underscore (`_`) styles, adjusting table names appropriately (e.g. `ipv_conversation_sessions`) to enable clean multi-agent database cohabitation.
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
