# 01-foundations / Testing and Verification

`agno-harness` has a strict test bar. **Unit tests must not call a real model over the network**.

---

## 1. Principles and toolchain

- **Package and run**: `uv`, then `make check` (`ruff` lint and format, `mypy` types, full `pytest`);
- **Mock agent**: the agent only needs `arun(**kwargs)` to return an async-iterable of Agno chunks. Use the `FakeAgent` fixture for a scripted, deterministic run;
- **Layering scan**: `tests/test_layering.py` walks the AST so `core/` and `runtime/` cannot import FastAPI or channel SDKs the wrong way.

---

## 2. Common unit-test patterns

### 2.1 Protocol timing (`SequencerMode`)

- `SequencerMode.STRICT`: a protocol violation (content before `TEXT_MESSAGE_START`) fails the test;
- `SequencerMode.AUDIT`: production default; repairs out-of-order frames silently and records repair metrics.

```python
from agno_harness import AgentRuntime, SequencerMode
from tests.conftest import FakeAgent, content, run_completed, make_input

runtime = AgentRuntime(
    agent=FakeAgent([content("hello"), run_completed()]),
    sequencer_mode=SequencerMode.AUDIT,
)
events = [e async for e in runtime.stream_events(make_input())]
assert [e.type for e in events] == [
    "RUN_STARTED",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "RUN_FINISHED",
]
```

### 2.2 Mount and identity tests

Check FastAPI mount and 401 rejection:

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient
from agno_harness import RelayApp

relay = RelayApp(runtime)
app = FastAPI()
app.include_router(relay.get_router(resolve_user_id=lambda req: req.headers.get("X-User-Id")))

client = TestClient(app)
# No auth header → 401
assert client.post("/agui", json={...}).status_code == 401
# Valid auth header → 200
assert client.post("/agui", headers={"X-User-Id": "u1"}, json={...}).status_code == 200
```

### 2.3 Replace resolvers on a Golden Trace

Recording or replaying card frames against a production resolver hits a real DB or the network; the Golden jitters. Swap the whole map in tests:

```python
catalog.replace_resolvers({
    "movie": lambda data: {"title": "Alien", "rating": 8.4, "year": 1979},
})
```

`data` is still the model id; `resolved` becomes the fixture. Contract and JIT: [07 Skills](../02-interactions/07-skills-and-jit.md).

### 2.4 HITL

Have `FakeAgent` emit `run_paused`, then resume with a **trailing** `role: tool`. Cover all four `pauseType`s. Resume must write `TOOL_CALL_RESULT` for the same `toolCallId`. Answer JSON: [HITL 01](../02-interactions/02-hitl-and-actions/01-protocol.md).

### 2.5 Long runs

`LongRunManager` plus `InMemoryRunEventLog` (or the in-memory `Stores` stand-in). Assert: after `POST ?long-run=1`, dropping HTTP still leaves `/active` running; `/attach?after=` continues; **a paused tail must finish**, or the test hangs. Stop yields `CUSTOM run.cancelled`. Routes: [persistence 02](05-persistence-and-longruns/02-longrun-routes.md).

Real-LLM tests use the pytest marker `integration`. Do not run them by default.
