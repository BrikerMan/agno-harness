# 01. Dual storage

Complete history is **Agno `db` + harness `Stores`**. Neither substitutes for the other.

- **Agno `db`** (`SqliteDb` / `PostgresDb` on `Agent` and `AgentRuntime`) — next-turn messages. Without it the model starts each turn cold, even if frames are on disk.
- **Harness `Stores`** — the stream the user saw. Without it `/api/v1/threads/{id}/frames` cannot rebuild cards / tools / reasoning the way they were sent.

If harness stores are durable SQL and Agno `db` is missing or in-memory, `AgentRuntime` raises `StoragePairingError` unless `allow_ephemeral_agno_db=True`.

Inside harness stores there is a second split:

- **Hot log** (`RedisRunEventLog`, or an in-process stand-in) — every AG-UI delta, a blocking tail, TTL. `?long-run=1` and `GET /api/v1/runs/{id}/attach` read it. Do not store tokens in SQL.
- **History archive** (SQL) — the folded event list after the run. Consecutive content deltas become one frame. `GET /api/v1/threads/{id}/frames` reads it.

---

## Architecture: Dual-Database Setup

Agno native database and Harness database have clear boundaries:

### 1. Database Definition (`base.py`)

```python
from agno.db.sqlite import SqliteDb
from agno_harness.db import AgnoHarnessSqliteDb

# 1. Agno DB (model conversation context)
agno_db = SqliteDb(db_file="data/agent.db")

# 2. Harness DB (UI event stream, interactive cards, audit log, sessions)
agno_harness_db = AgnoHarnessSqliteDb(db_file="data/agent.db", prefix="admin_agent")
```

For PostgreSQL:
```python
from agno.db.async_postgres import AsyncPostgresDb
from agno_harness.db import AgnoHarnessPostgresDb

# Standalone URL mode
agno_harness_db = AgnoHarnessPostgresDb(db_url=settings.database_url, prefix="admin_agent")

# Or reuse an existing SQLAlchemy async_session_factory
agno_harness_db = AgnoHarnessPostgresDb.from_session_factory(
    db.async_session_factory,
    prefix="admin_agent",
)
```

### 2. Agent Binding (`agent.py`)

The agent only cares about its Agno native DB:
```python
the_agent = Agent(
    name="agent",
    description="agent",
    db=agno_db,
)
```

### 3. Runtime Setup (`main.py` / `runtime_factory.py`)

```python
runtime = AgentRuntime(
    agent=the_agent,
    harness_db=agno_harness_db,
    catalog=CARD_CATALOG,
)
```

`AgentRuntime` automatically:
1. Aligns `harness_db.prefix` to `agent.db`.
2. Assembles and binds default SQL stores.
3. Verifies or initializes the 8 core persistence tables.

---

## Agno v3 storage: runs live in their own table

Since Agno v3 a session row no longer carries its history. Each run is a row in `<prefix>_runs` (`agno_runs` by default); `session.runs` is rebuilt from that table when you read a session.

What this means for harness:

- `db.upsert_session(session)` **does not write runs**. Editing `run.metadata` or `run.messages` and then upserting the session loses the edit. Use `db.upsert_run(run, session_id=..., user_id=...)`.
- `seal_session_run` and `attach_checkpoint_to_session` already do this (sync and async dbs). Backends that have not been ported raise `NotImplementedError` from `upsert_run`; the harness then falls back to `upsert_session`.
- If you query the old `runs` column of the sessions table directly (dashboards, exports), read `<prefix>_runs` instead (`run_id`, `session_id`, `run_index`, `run_data`).
- Cancelled and errored runs are written by Agno from its own cleanup path. A seal that races it waits briefly (up to about half a second) for the run to appear before it falls back to the latest run.

## Upgrading an Agno v2 database

Agno v3 ships a non-destructive migration: it copies each run from the old `runs` JSON column into `<prefix>_runs`, adds `user_id` columns, and leaves the legacy column in place as a backup. Re-running it never duplicates runs.

1. Take a restorable backup and stop every writer (the app, workers, schedulers).
2. Migrate:

   ```bash
   agno-harness db migrate-agno --sqlite data/agent.db
   agno-harness db migrate-agno --url postgresql+psycopg://user:pass@host/db --prefix admin_agent
   ```

   Or from code (works with sync and async Agno dbs):

   ```python
   from agno_harness import migrate_agno_db

   report = await migrate_agno_db(agno_db)
   print(report.version_before, "->", report.version_after, report.sample_run_ids)
   ```

3. Compare the full legacy history with `<prefix>_runs`, including sessions outside the sample. A sample proves nothing about completeness.
4. Only after that, and with a backup you can restore, consider Agno's `cleanup_legacy_runs_column` (SQL) or `cleanup_legacy_runs_field` (document / key-value stores). agno-harness never calls cleanup and never passes `force=True`.

`AgnoHarnessDb.from_agno_db(...)` logs a warning at startup when the paired Agno database still has the v2 schema.

`--prefix` must match the prefix your app uses (`AGNO_HARNESS_TABLE_PREFIX` or the `prefix=` you pass), because the prefix also renames Agno's own tables. If the app passes a custom `session_table=` to `SqliteDb` / `PostgresDb`, pass the same name with `--session-table`; otherwise the command looks at `agno_sessions`, finds nothing, and reports that no migration is needed. Vector stores are migrated separately; see the [Agno v3 migration guide](https://docs.agno.com/other/v3-migration).

---

## Two Initialization Modes

### Mode A: Default Quickstart (Silent Auto-create with Warning)

Best for local development, prototyping, and testing:
- `auto_create=True` (default).
- Automatically creates missing harness tables on startup.
- Emits a warning log recommending migration tools for production:
  > `[agno-harness] ⚠️ Initialized harness tables automatically for prefix 'admin_agent'. For production environments, it is recommended to manage schema versions via AlembicMigrator.declare_models(Base).`
- If an `alembic_version` table is detected, auto-create is skipped automatically to avoid generating empty autogenerate diffs.

### Mode B: Enterprise Alembic Migrations

For production deployments or existing Alembic repositories:

```python
# app/models/__init__.py (imported by alembic/env.py)
from agno_harness.db import AlembicMigrator
from app.models.base import Base

# Single agent
AlembicMigrator.declare_models(base=Base, prefix="admin_agent")

# Multiple agents sharing one database
AlembicMigrator.declare_models(base=Base, prefix="user_agent")
```

Run standard migrations:
```bash
alembic revision --autogenerate -m "add harness tables"
alembic upgrade head
```

In production, configure `auto_create=False` on `AgnoHarnessDb`. If any table is missing, startup raises `MissingHarnessTablesError` with actionable instructions.

---

## Table Prefix and Multi-Agent Isolation

Prefixes support letters, numbers, underscores `_`, and hyphens `-` (e.g. `admin_agent` or `admin-agent`).
Prefixes containing underscores use underscore separators (e.g. `admin_agent_conversation_sessions`), aligning with PostgreSQL conventions.

The 8 persistence tables:
1. `threads` (first-class thread entity & UI lifecycle status)
2. `conversation_sessions`
3. `actions`
4. `message_audits`
5. `custom_events`
6. `run_frames`
7. `run_records`
8. `run_archives`

---

## Architectural Principles: Database is Mandatory, Redis is Optional (Memory Alternative Supported)

1. **Relational Database (SQLite / PostgreSQL) is strictly mandatory**:
   - Running without a database is not supported.
   - All thread entities, UI states (`threads`), session management, audit logs, and frame archives are stored in the database.
   - `GET /api/v1/threads` reads directly from the `threads` table in milliseconds rather than deserializing full Agno session runs.
2. **Redis is optional, with an in-memory alternative**:
   - Redis is only required for high-throughput streaming deltas, SSE attach, and distributed multi-worker resume.
   - For single-process, local development, or lightweight deployments, `InMemoryRunEventLog` serves as the in-process alternative to Redis, without requiring a Redis server.

---

## Streams and Resumes (`X-Agui-Resume`)

On `Stores`, `event_log` and `event_stream` are separate: SQL log alone is history; add Redis for live. `resume_mode` is derived from that.

| What you mounted | `X-Agui-Resume` | Behavior |
| --- | --- | --- |
| Nothing | `none` | Drop the connection = stop the run |
| `event_log` only | `history` | Finishes in the background; no mid-sentence follow |
| Plus `event_stream` (Redis or equivalent) | `live` | `after=` resumes mid-sentence |

Cross-process live needs real Redis. `memory://` is this process only. A failed log write is `unrecordable`; the stream is not killed.

Never mix cursors: SSE `id:` is a one-run log offset (for **attach**); a frames id is `{runId}:{paddedOffset}` (for `/frames` only). See [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md).

Next: [02 Long-run routes](02-longrun-routes.md).
