# 01. Dual storage

- **Hot log** (`RedisRunEventLog`, or an in-process stand-in) — every AG-UI delta, a blocking tail, TTL. `?long-run=1` and `GET /runs/{id}/attach` read it. Do not store tokens in SQL.
- **History archive** (SQL) — the folded event list after the run. Consecutive content deltas become one frame. `GET /threads/{id}/frames` reads it.

The base does not create tables. Mix the mixins onto your `Base` and run `create_all` / Alembic yourself.

```python
from agno_harness.stores import (
    DefaultRelayBase,
    RunArchiveMixin,
    RunFrameMixin,
    RunRecordMixin,
    SessionRecordMixin,
    Stores,
    SQLAlchemyActionStore,
)
```

On `Stores`, `event_log` and `event_stream` are separate: SQL log alone is history; add Redis for live. `resume_mode` is derived from that.

| What you mounted | `X-Agui-Resume` | Behavior |
| --- | --- | --- |
| Nothing | `none` | Drop the connection = stop the run |
| `event_log` only | `history` | Finishes in the background; no mid-sentence follow |
| Plus `event_stream` (Redis or equivalent) | `live` | `after=` resumes mid-sentence |

Cross-process live needs real Redis. `memory://` is this process only. A failed log write is `unrecordable`; the stream is not killed.

Never mix cursors: SSE `id:` is a one-run log offset (for **attach**); a frames id is `{runId}:{paddedOffset}` (for `/frames` only). See [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md).

Next: [02 Long-run routes](02-longrun-routes.md).
