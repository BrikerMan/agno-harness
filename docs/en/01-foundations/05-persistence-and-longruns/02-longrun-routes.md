# 02. Long-run routes

```python
from agno_harness import LongRunManager, make_agui_router

long_runs = LongRunManager(runtime, log=stores.event_log, stream=stores.event_stream)
app.include_router(make_agui_router(runtime, long_runs=long_runs, resolve_user_id=...))
# lifespan exit:
await long_runs.shutdown()
```

On the Relay path, `relay.get_router(...)` takes the same `long_runs`. Without it, `/runs/*` is **404**.

| Route | Role |
| --- | --- |
| `POST /agui?long-run=1` | Run in the background; tab close does not stop it (`?long_run=1`, legacy `?detach=1`) |
| `GET /runs/{id}/attach?after=` | **Canonical** rejoin of a producing run |
| `GET /runs/{id}/stream?after=` | Deprecated, same as `/attach` |
| `GET /threads/{id}/active` | Still running **or HITL paused** (`is_open`), includes `input`. A paused run is not producing (`is_producing` false); attach/tail ends after stored frames |
| `POST /runs/{id}/abort` | User Stop. Body `{ aborted: bool }` |

Tab close / crash ≠ abort. One open run per thread. `start` is idempotent for the same `runId`. A fresh client `newId("run")` does not stop an already long-running job.

| Layer | Mechanism | Role |
| --- | --- | --- |
| Wire | SSE comment `: ping` (first at 0s, about every 5s) | Keep Nginx/ALB from cutting TCP; not a reducer event |
| Worker | Redis beat TTL | Whether this process is still writing the log |
| Resume | `GET /runs/{id}/attach?after=` | Continue after drop / refresh |

The client resets idle on any byte (including ping) and re-attaches after about 3× silence. Implementation lives only in [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md).

Next: [03 FAQ](03-faq.md).
