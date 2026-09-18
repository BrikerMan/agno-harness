# frontend-kit

Copy this directory into the product. **Not an npm package.**

This is the AG-UI protocol layer for `agno-harness`: one `applyEvent` for live SSE, `/frames` replay, and `/attach` resume. It is the runnable form of the [Web / React five steps](../../docs/en/03-clients/01-web-react/README.md).

It is **not** a chat UI. Sidebar, Composer, bubbles, and card renderers stay in the product.

## Copy

```text
frontend-kit/
  agui.ts                 types + body.order[]
  apply-event.ts          pure reducer (live / frames / attach)
  sse.ts                  fetch SSE, id: cursor, : ping idle
  use-agui-chat.ts        send / stop / attach / HITL resume
  use-stick-to-bottom.ts  pin on send; unpin only on user scroll-up
  index.ts
```

Peer: React 18+. No other runtime dependency.

```ts
import { useAguiChat } from "./frontend-kit";

const chat = useAguiChat({
  apiBase: "/api",                 // Vite proxy strips /api → backend root
  storageKey: "my-app:session",
  getHeaders: () => ({ "X-User-Id": userId }),
  debug: import.meta.env.DEV,
});
```

Vite:

```ts
proxy: {
  "/api": {
    target: "http://localhost:8000",
    changeOrigin: true,
    rewrite: (p) => p.replace(/^\/api/, ""),
  },
}
```

## Server contract

Wire frames are AG-UI `EventEncoder` payloads: `{ "type": "RUN_STARTED", ... }`.
Not `{ "event": "run_started" }`.

```python
from agno_harness import AgentRuntime, make_agui_router
from agno_harness.runtime.longrun import LongRunManager

app.include_router(
    make_agui_router(
        runtime,
        long_runs=LongRunManager(runtime, log=hot, stream=hot),
        resolve_user_id=resolve_user,
        include_health=True,          # GET /health → { resumeMode }
        expose_debug_routes=True,     # optional DevTools chunks
    ),
    prefix="/api",                    # or proxy /api → "" and omit prefix
)
```

`make_relay_router` / `relay.get_router()` already mounts `GET /health` with `resumeMode`.

The hook **must** see `resumeMode` before the first send. Missing field → treated as `"none"` → refresh never attaches.

| Client | Server |
| --- | --- |
| `GET {apiBase}/health` | `{ status, resumeMode: "none" \| "history" \| "live" }` |
| `POST {apiBase}/agui?long-run=1` | SSE. Header `X-Agui-Resume`, `X-Agui-Protocol: 1.0` |
| `GET {apiBase}/runs/{id}/attach?after=` | live tail from the SSE `id:` cursor |
| `POST {apiBase}/runs/{id}/abort` | Stop only — not tab close |
| `GET {apiBase}/threads/{id}/frames` | replay through the same `applyEvent` |
| `GET {apiBase}/threads/{id}/active` | running / paused |

Identity is a header the server trusts (`resolve_user_id`). Never a `userId` field in `RunAgentInput`.

## Do not

- Rebuild bubbles from `GET /threads/{id}/messages`
- Paint tools outside `body.order[]`
- `POST .../abort` on tab close
- Mix attach `id:` with frames `{runId}:{offset}`
- Treat `: ping` as an AG-UI event, or abort the run on idle

Product shell (titles, four-state sidebar, HITL forms, cards): still the five steps. This kit only owns the stream.
