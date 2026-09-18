# 01-foundations / FastAPI Integration

In enterprise stacks the agent is usually not a lone microservice. It mounts onto an existing gateway, auth system, and service mesh.

`agno-harness` has **two integration modes**:
1. **Primary (recommended): `relay.get_router()` / `make_relay_router()` mount**;
2. **Secondary (standalone): subclass `RelayServer`**.

---

## 1. Primary mode: mount on an existing FastAPI app

This is the recommended production path. Your backend already has routes, middleware, metrics, and JWT auth.

Two mount paths, depending on the product:

### Path A: Web / desktop only

For a desktop sidecar or a Web Agent API, mount `AgentRuntime` with `make_agui_router`:

```python
from fastapi import FastAPI
from agno_harness import AgentRuntime, LongRunManager, make_agui_router

app = FastAPI(title="Agent Sidecar")

# 1. Runtime and LongRunManager
runtime = AgentRuntime(agent=agent, db=db, catalog=catalog)
long_runs = LongRunManager(runtime)

# 2. Standard AG-UI router
agui_router = make_agui_router(
    runtime,
    long_runs=long_runs,
    resolve_user_id=resolve_local_user,
    expose_debug_routes=True,
    include_health=True,
)
app.include_router(agui_router)
app.include_router(agui_router, prefix="/api")
```

### Path B: Multi-channel (Teams, Lark, CLI, Web)

To ship the same agent to enterprise IM (Teams / Lark) and Web, use `RelayApp` with `make_relay_router` (or `relay.get_router()`):

```python
from fastapi import FastAPI, Request
from agno.agent import Agent
from agno_harness import AgentRuntime, RelayApp, TeamsChannel, LarkChannel

# 1. Execution kernel (standard AG-UI stream)
runtime = AgentRuntime(agent=agent)

# 2. RelayApp gateway and channels
relay = RelayApp(runtime=runtime)
relay.add_channel(TeamsChannel(bot_app_id="...", bot_app_password="..."))
relay.add_channel(LarkChannel(app_id="...", app_secret="..."))

app = FastAPI(title="Enterprise Bot Gateway", lifespan=relay.lifespan)

# 2. Server-side identity (fail-fast and fail-loud)
def resolve_enterprise_user(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    return verify_jwt_and_get_user(auth)

# 3. One mount for Web AG-UI and every channel webhook
app.include_router(
    relay.get_router(
        prefix="/agent",
        resolve_user_id=resolve_enterprise_user,
        tags=["AI Agent"],
    )
)
```

### What the mount gives you

- `GET /agent/health`: readiness probe; `{ status, channels, resumeMode }` (`none` / `history` / `live`). The React kit reads `resumeMode` before the first send.
- `POST /agent/agui`: standard AG-UI SSE pipe;
- `GET /agent/threads`: thread list isolated to the authenticated user;
- `GET /agent/threads/{id}/messages`: history replay;
- If you `relay.add_channel(teams_channel)`, the Teams webhook is ready as well.

---

## 2. Identity: fail-fast and fail-loud

Security first: **never trust a user ID from the client body**.

### Startup validation

If you call `relay.get_router()` or `make_relay_router()` without `resolve_user_id`, and you do not set `allow_anonymous=True`, startup **raises `ConfigurationError`**:

```python
# Wrong: no identity resolver — startup fails
router = relay.get_router()  
# ConfigurationError: resolve_user_id is required in production to authenticate callers...

# Local debug: anonymous mode, explicit
router = relay.get_router(allow_anonymous=True)

# Production: inject auth
router = relay.get_router(resolve_user_id=resolve_user)
```

### Runtime 401

On `POST /agui` or `GET /threads`:
- If `resolve_user_id(request)` returns `None`, the gateway **returns `HTTP 401 Unauthorized` immediately**;
- It never falls back to anonymous execution, so an unauthenticated caller cannot read or write another session.

---

## 3. Secondary mode: standalone `RelayServer`

For a dedicated agent microservice, `RelayServer` is an OOP subclass:

```python
from agno_harness.server import RelayServer
from fastapi import Request

class MyEnterpriseServer(RelayServer):
    def resolve_user_id(self, request: Request) -> str | None:
        """Override identity resolution."""
        return request.headers.get("X-Staff-Id")

    def setup_middleware(self) -> None:
        """Add enterprise audit middleware."""
        super().setup_middleware()
        # self.add_middleware(...)

    def setup_routes(self) -> None:
        """Add custom routes."""
        super().setup_routes()
        
        @self.get("/api/v1/ping")
        async def ping():
            return {"pong": True}

# Instantiate and run
server = MyEnterpriseServer(relay)
if __name__ == "__main__":
    server.run(host="0.0.0.0", port=8000)
```
