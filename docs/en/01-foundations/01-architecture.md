# 01-foundations / Architecture Overview

`agno-harness` is enterprise agent scaffolding: **Agno Runtime + Relay**. Runtime runs the agent and reconstructs the UI. Relay takes the same code into Lark / Teams / CLI. You write the business and the cards.

---

## 1. Architecture overview

The system uses strict architectural layering so imports stay one-way and boundaries do not leak:

```
+-----------------------------------------------------------------------------------+
|                            Enterprise host                                        |
|   Existing FastAPI App (app.include_router(relay.get_router()))  /  RelayServer   |
+-----------------------------------------------------------------------------------+
                                         │
+-----------------------------------------------------------------------------------+
|                        RelayApp (multi-channel gateway)                           |
|  - Webhook Idempotency (DeduplicationCache)                                       |
|  - Reaction Lifecycle Safety (try-finally settle ✅/❌)                           |
|  - Interactive Action Dispatcher (@relay.action)                                  |
|  - Session Lifecycle & Topology (SessionManager + User-defined SessionKeyResolver)|
+-----------------------------------------------------------------------------------+
         │                                                            │
         ▼                                                            ▼
+---------------------------------------+           +-------------------------------+
|         Transport Channels            |           |         Message Sinks         |
|  - Web (SSE / Wire Protocol)          |           |  - InMemorySink               |
|  - CLI (Rich Console)                 |           |  - SQLiteSink                 |
|  - Teams (Bot Framework / Adaptive)   |           |  - PostgresSink               |
|  - Lark (Event WebSocket / Cards v2)  |           |                               |
+---------------------------------------+           +-------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
|                       AgentRuntime (execution kernel)                              |
|  - EventTranslator & Sequencer (timing repair state machine)                      |
|  - SmartCompressionManager & In-Context Checkpointing                             |
|  - Clean Seal Engine (unclosed tool-call completion and seal)                     |
|  - First-Class Observability (Langfuse / OpenInference tag injection)             |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
+-----------------------------------------------------------------------------------+
|                             AGNO Agent engine                                     |
|                   agno.agent.Agent.arun() + Model Providers                       |
+-----------------------------------------------------------------------------------+
```

---

## 2. Names and responsibilities

In `agno-harness`, each core component has a single job:


| Component                                 | Responsibility                                                                          | Typical call                                                  |
| ------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------- |
| `RelayApp`                            | Multi-channel gateway. Consumes the `AgentRuntime` AG-UI stream, orchestrates Channel, SessionManager, and Sinks, and provides webhook idempotency, reaction ACK, and card action routing. | `relay = RelayApp(runtime=runtime)`                     |
| `make_relay_router`                   | **Recommended FastAPI mount.** Packs the full AG-UI protocol and channel webhooks into a standard `APIRouter`.        | `app.include_router(relay.get_router(...))`             |
| `RelayServer`                         | **Batteries-included FastAPI server class.** OOP inheritance for a standalone gateway microservice.                            | `class MyServer(RelayServer): ...`                      |
| `AgentRuntime`                        | Agent execution kernel. Drives an Agno Agent/Team and compiles reasoning, tools, and cards into a standard AG-UI event stream. | `runtime = AgentRuntime(agent=my_agent)`                |
| `CardCatalog`                         | *Card metadata and cross-channel render orchestrator for Class-First card components.*                                       | `catalog = CardCatalog([MovieCard])`                    |
| `ConversationKey`                     | Typed descriptor of session topology on Web/CLI/Teams/Lark.                                          | `key = ConversationKey(platform="teams", ...)`          |
| `SessionManager`                      | Session persistence and 25-hour idle timeout. Supports a user-defined `SessionKeyResolver`.                              | `sess_id, is_new = await sm.get_or_create_session(key)` |


---

## 3. Layering guardrails

`tests/test_layering.py` enforces these boundaries automatically:

1. `core/` **has no external runtime deps**:
  - Import the standard library and Pydantic only.
  - **Do not** import `agno`, `fastapi`, channel SDKs, or database drivers.
2. `runtime/` **is execution only**:
  - It may import `agno` to run inference. **Do not** import `fastapi` or other web libraries.
3. `channels/` **are orthogonal**:
  - Web, CLI, Teams, and Lark must not call each other directly.
  - External SDKs (`botbuilder-core`, `lark-oapi`) load lazily.
4. `sinks/` **are decoupled from the hot path**:
  - Audit writes are async from the main run. A failed audit write must not interrupt the user stream.

---

## 4. Sequencer / Module / test layering

`EventSequencer` repairs AG-UI order inside runtime (missing START, close an out-of-order frame). Production default is `SequencerMode.AUDIT`. Wire events and violation shapes: [layer 3 / 02](../04-deep-dive-and-faq/02-protocol-wire-spec.md).

A `Module` (StreamUI, SubAgent, Observability, CustomEvents) owns its `CUSTOM` namespace and does not import the others. Prefer a new Module over more branches in the translator.

Layering is enforced by `tests/test_layering.py` walking the AST. FakeAgent / Golden: [06 Testing](06-testing-and-verification.md).

---

## 5. Three ways to run, and one-turn lifecycle

You can skip a lower layer. Three call styles:

| Style | You write | When |
| --- | --- | --- |
| **Hosted** | `runtime.stream_events(run_input)` | Web / IM / the default path |
| **Semi-hosted** | Plus pre-run / post-run hooks that mutate `RunScope` | Titles, checkpoints, stamping `user_id` |
| **Manual** | Your own `agent.arun()`, chunks into `EventTranslator` | You already have an Agno loop and only want AG-UI frames |

One turn:

```text
RUN_STARTED
  → pre-run hooks
  → optional STATE_SNAPSHOT
  → each Agno chunk: official HANDLERS → parsers (append only) → modules → sequencer
  → post-run hooks
  → RUN_FINISHED / RUN_ERROR
```

A dropped client has no terminal event (on `none`, set `isStreaming=false` yourself). How a parser yields `CUSTOM`: [07 Writing an agent](07-writing-an-agent.md).

`BridgeModule`: a name plus an optional `CUSTOM` namespace. Same name / namespace fails at assemble time. `CUSTOM` is invisible to the sequencer; the module must close its own brackets in `on_run_finish`. `StreamUIModule` owns `<stream-ui>`, per-line `ui.item`, `emit_text`, and `artifact_root_dir` templates with path-traversal checks.
