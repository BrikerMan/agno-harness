# 01-foundations / Persistence and long runs

Complete history is **two databases**. Missing either one is incomplete.

| Store | What it remembers | Without it |
| --- | --- | --- |
| **Agno `db`** (`SqliteDb` / `PostgresDb`) | Next-turn model context (`agno_sessions` / `agno_runs`) | The UI can replay; the model forgets |
| **Harness `Stores`** (SQL `event_log` / `history_archive`) | The stream the user saw (frames, cards, custom events) | The model remembers; refresh loses cards / tools |

`AgentRuntime` **fails** if harness stores are durable SQL and Agno `db` is missing or in-memory, unless you pass `allow_ephemeral_agno_db=True`.

The Agno session is for the **next model turn**, not the stream the user saw. If a refresh must still show cards / child panels, or a run must outlive that HTTP request, use two stores plus `LongRunManager`.

Frontend idle reconnect → [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md). This series is routes and storage duties only.

| Step | Contents |
| --- | --- |
| [01 Dual storage](01-dual-storage.md) | Hot log vs archive; mixins; `X-Agui-Resume` |
| [02 Long-run routes](02-longrun-routes.md) | `LongRunManager`, `/attach`, `/active`, `/abort`; ping vs Redis beat |
| [03 FAQ](03-faq.md) | Missing prompt, blank bubble, Stop, `runCount` |
