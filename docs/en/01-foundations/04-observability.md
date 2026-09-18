# 01-foundations / First-Class Observability

In production, **observability is a first-class concern**.
When an agent runs across channels, tools, and delegated sub-agents, operators need a trace that answers:
1. Which user started this session, and on which surface (Teams / Lark / Web)?
2. Which stage was slow (TTFT, tool latency, sub-agent work)?
3. Which production requests needed protocol repair or were cut short?

`agno-harness` connects to **OpenTelemetry** and **Langfuse / Phoenix** (OpenInference) out of the box.

---

## 1. Langfuse in a few env vars

Set the standard variables. On startup the gateway detects them and opens an OTLP export path:

```bash
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."
export LANGFUSE_BASE_URL="https://cloud.langfuse.com" # or a private host
export OTEL_SERVICE_NAME="enterprise-agent"
export OTEL_ENVIRONMENT="production"
```

Two stacks, no double-count of prompt / tools:

- **`setup_otlp()`** installs `openinference-instrumentation-agno`: model, tools, tokens.
- **`ObservabilityModule`**: one root span per AG-UI run (default `<agent-name>.turn-run`) with user-visible input/output, TTFT, frame counts, and whether the client is still attached. It does not re-record prompt / tools.

No collector configured → no-op. That is **not** the local JSONL dump: `AGNO_HARNESS_TRACE_DIR` writes chunk → event pairs for parsers / Goldens. OTLP is for Langfuse / Phoenix. Debug protocol against the directory; read production boards from the collector.

Or initialize in code:

```python
from agno_harness.observability import setup_otlp

setup_otlp(
    service_name="enterprise-agent",
    environment="production",
    langfuse_public_key="pk-lf-...",
    langfuse_secret_key="sk-lf-...",
    langfuse_base_url="https://cloud.langfuse.com",
)
```

---

## 2. Root span name: `<agent-name>.turn-run`

Each AG-UI run opens one **root span**. The default name is always:

```text
<agent-name>.turn-run
```

`agent-name` comes from `Agent.name` (whitespace collapsed to `-`, lowercased). Unnamed agents use `agent`.

| Agent | Root span name (default) |
| --- | --- |
| `Agent(name="movie-assistant")` | `movie-assistant.turn-run` |
| `Agent(name="Movie Bot")` | `movie-bot.turn-run` |
| `Agent()` with no `name` | `agent.turn-run` |

```python
from agno_harness import ObservabilityModule

# Agent(name="movie-assistant") → "movie-assistant.turn-run"
runtime.register_module(ObservabilityModule().bind_agent(agent))
```

- **Do not** put a run id or UUID in the name. Backends bucket by span name; a unique name per run is the same as no grouping.
- Put `run_id`, `thread_id`, `user_id`, and `channel_id` on **attributes**.
- Pass `span_name=` only when you need to split kinds of conversation (DM vs group). That callback must stay low-cardinality too.

---

## 3. Multi-dimensional attribute matrix

`agno-harness` follows OpenInference and OpenTelemetry. Each agent run injects these fields so you can filter in Langfuse:

| Attribute key | Meaning | Filter value |
| :--- | :--- | :--- |
| **`session.id`** | Unified session ID (Agno session ID / thread_id) | Full multi-turn trace tree for one session |
| **`user.id`** | Authenticated user primary key | One user's behavior and incidents |
| **`platform`** | Origin channel (`web`, `teams`, `lark`, `cli`) | Compare usage and error rate by surface |
| **`deployment.environment`** | Runtime (`production`, `staging`, `dev`) | Separate prod from test traffic |
| **`tags` / `langfuse.tags`** | Custom tag array (for example `["vip", "billing"]`) | Business-dimension boards |
| **`input.value`** | This turn's user prompt | Inspect model input |
| **`output.value`** | Final text sent to the user | Quality and compliance review |
| **`agui.ttft_ms`** | Time to first token (ms) | Core latency SLO |
| **`agui.duration_ms`** | Full interaction duration (ms) | Throughput and latency |
| **`agui.cards`** | Structured UI cards in this turn | Rich-UI adoption |
| **`agui.tool_calls`** | Tool calls in this turn | Planning complexity and loop risk |

---

## 4. Async span detach (`patch_context_detach`)

In a busy `asyncio` loop — streaming `async for` and background tasks — native OpenTelemetry context often drops across coroutines, so child spans do not hang under the parent.

`agno-harness` ships `patch_context_detach()`. It keeps context intact across channel hops so sub-agent, tool, and card-parse spans stay on the same root trace.
