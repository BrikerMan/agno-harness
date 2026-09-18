# 01. Why SmartCompressionManager, and `num_history_runs`

Long multi-turn work, todo plans, and frequent tool calls usually hit:

1. **Context overflow or exploding cost** — full history plus long tool results grow tokens exponentially.
2. **Mechanical amnesia** — Agno’s native compressor issues many sync LLM calls; a sliding window drops early history.

## 1. Gaps in the native CompressionManager

| Axis | Agno native | `SmartCompressionManager` |
| --- | --- | --- |
| Grain | Only `role: "tool"`; cannot compact User / Assistant | Whole turns and tool products fold into one checkpoint |
| Stall | One sync completion per tool result; stream freezes 10–20s | One checkpoint when over limit; usually 1–2s |
| KV cache | Fresh prompt every time; prefix cache miss | Append the prompt to the existing messages; prefix reusable |
| Semantics | Isolated tool dumps; loses “what the user wanted” | Summarizes intent, decisions, facts, next step |
| Observability | No dedicated span | `context_compression` span with before/after tokens |

## 2. Set `num_history_runs` to 100

```python
agent = Agent(
    ...,
    add_history_to_context=True,
    num_history_runs=100,  # not 0, not None
    compress_tool_results=True,
    compression_manager=compression_manager,
)
```

| Setting | Agno internals | History loaded | Verdict |
| --- | --- | --- | --- |
| **`0`** | `last_n_runs <= 0` → `return []` | **0 turns** | Stateless; the compressor never sees history |
| **`None`** | `__init__` rewrites `None` to **3** | **Hard window of 3** | Turn 4 drops early constraints; tokens often never hit the trigger |
| **`100`** | `runs[-100:]` | Last 100 turns | Memory is the compressor’s job; checkpoint then prune |

`0` makes `AgentSession.get_messages` return `[]`. `None` is not “unlimited” — Agno forces 3.

`100` will **not** blow the window. Adaptive prune:

```text
Turns 1–3: 2k → 6.8k < 7k → SKIP, keep the full dialogue
Turn 4: 8.5k >= 7k → emit # CONTEXT CHECKPOINT, rebuild to ~1.8k
Turn 5+: load the latest checkpoint, drop raw turns before it
Even with num_history_runs=100, the model sees checkpoint + recent turns, ~2k
```

Next: [02 Checkpoint and config](02-checkpoint-and-config.md).
