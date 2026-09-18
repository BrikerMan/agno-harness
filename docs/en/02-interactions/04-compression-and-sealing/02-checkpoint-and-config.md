# 02. Checkpoint and config

## 1. Runtime sequence

```text
Pre-flight → SmartCompressionManager.should_compress()
        tokens < trigger → skip, this-turn LLM
        tokens >= trigger → acompress()
            1. Reuse the history prefix (KV cache)
            2. Emit a structured checkpoint
            3. Reorganize messages in place
        → this-turn LLM → PostRunHook → attach_checkpoint_to_session()
```

The preflight uses local `cl100k_base` over messages plus tool JSON schemas, usually `< 1ms`. With `debug_mode=True`:

```text
INFO [SmartCompression] Pre-LLM Check: 11 msgs, 6,877 total tokens
     (msgs: 4,680 + tools: 2,197), trigger limit: 7000 -> SKIP
```

At the threshold, append `DEFAULT_IN_CONTEXT_CHECKPOINT_PROMPT` as a **temporary last user message** and `aresponse`. The prefix matches the just-cached prompt, so vLLM / compatible gateways can hit the prompt cache.

## 2. What the checkpoint must contain

`# CONTEXT CHECKPOINT` has five blocks:

1. **User Intent & Constraints** — goal, delivery format, hard limits.
2. **Key Decisions & Rationale** — agreements and why.
3. **Discovered Facts & Ground Truth** — no “looked up the weather”; exact numbers and entities.
4. **Completed Actions & Findings** — tools used, artifacts, errors.
5. **Active State & Immediate Next Steps** — progress and the next required step.

Reorganize: keep the first system message; inject `[Context Checkpoint]` as **user** plus an assistant ack; keep the active turn’s user / tool messages; drop earlier turns.

Why `user` not `system`: many OpenAI-compatible endpoints allow `system` **only at index 0**. A mid-list `system` is a 400.

## 3. Wiring template

```python
import os
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai.like import OpenAILike
from agno_harness import AgentRuntime, SmartCompressionManager, make_checkpoint_hook

model = OpenAILike(
    id="qwen-plus",
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL"),
)
db = SqliteDb(db_file="data/sessions.db")

trigger_tokens = int(os.getenv("COMPRESSION_TRIGGER_TOKENS", "50000"))
compression_manager = SmartCompressionManager(
    model=model,
    trigger_token_limit=trigger_tokens,
    min_messages=4,
    debug_mode=True,
)

agent = Agent(
    name="researcher",
    model=model,
    db=db,
    tools=[...],
    instructions="...",
    add_history_to_context=True,
    num_history_runs=100,
    compress_tool_results=True,
    compression_manager=compression_manager,
    markdown=True,
    telemetry=False,
)

runtime = AgentRuntime(agent=agent, db=db)
runtime.on_post_run(make_checkpoint_hook(runtime))
```

| Param | Default | Role |
| --- | --- | --- |
| `model` | `None` | Model that writes the checkpoint; usually the main model |
| `trigger_token_limit` | `12000` | Trigger; see the matrix |
| `min_messages` | `4` | Do not compress tiny histories |
| `compress_tool_results` | `True` | `False` skips entirely |
| `debug_mode` | `False` | Print preflight and savings |
| `encoding_name` | `"cl100k_base"` | tiktoken |
| `checkpoint_instructions` | `None` | Custom prompt |

## 4. Threshold matrix

A 1M context window is **not** permission to snowball a multi-turn agent. Around 50k balances attention, latency, KV-cache memory, and the bill: dilution, the GPU memory wall, input tokens billed every turn, and TTFT after a long prefill.

| Scene | Recommended `trigger_token_limit` |
| --- | --- |
| 128k–1M flagship (prod) | **50,000** (50k–80k) |
| 128k default | **50,000** (40k–60k) |
| High-RPS router / sub-agent | **20,000** (15k–25k) |
| Local card preview | **7,000** (5k–8k) |

Set it from an env var. Compression span fields: `compression.original_tokens` / `compacted_tokens` / `saved_tokens` / `messages_before` / `messages_after` / `elapsed_seconds`.

## 5. Folded checkpoint card

A compression turn emits a `CUSTOM` (or StreamUI block) with before/after tokens. Paint a **folded checkpoint card**. Do not dump the whole checkpoint as a normal bubble.

- Default one line: `⚡ In-Context Checkpoint: 12.4k → 2.1k (-83%)`.
- Expand three numbers: before / after / saved, then Markdown for the full `# CONTEXT CHECKPOINT`.
- After refresh the same `applyEvent` can open the card (it landed in the archive).
- To see the card sooner locally, set `COMPRESSION_TRIGGER_TOKENS` to `5000`–`7000`. Production stays at 50k.

Next: [03 Seal, FAQ, and how you know the Web is right](03-seal-and-faq.md).
