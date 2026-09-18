# 03. Seal and FAQ

A refresh, drop, or killed process can leave “tool call started, no result” in history. The next turn then dies on an illegal message order.

```python
from agno_harness.runtime.closure import close_dangling_tool_calls, seal_session_run

seal_session_run(session)
```

The seal:

1. Finds tool calls with no matching result;
2. Inserts a synthetic cancel record (for example client disconnect);
3. Marks the run in a continuable terminal state;
4. Keeps the next-turn history on the OpenAI alternating contract.

Hang it on post-run / abnormal-exit. Orthogonal to compression: compression owns tokens; the seal owns dirty tool calls.

## FAQ

### Q1: Why does the console keep printing `SKIP (6,877 < 7,000)`?

Expected. `debug_mode=True` preflights every turn. Compression runs only after `trigger_token_limit`. To see the card sooner, temporarily set `COMPRESSION_TRIGGER_TOKENS=5000`.

### Q2: Can `num_history_runs` be `None`?

**No.** Agno rewrites it to `3`. Turn 4 drops early history and the compressor may never fire. Write `100` explicitly. See [01](01-why-and-num-history.md).

### Q3: Why is the checkpoint a `user` message?

Compatible gateways allow `system` only at the head of the list. A mid-list `system` is a 400. Use `[Context Checkpoint]` as user plus an assistant ack.

### Q4: Does the next turn still remember the user’s original constraints?

Yes — they live in the checkpoint’s Intent / Constraints / Ground Truth, not in the raw turn-1 bubble. If a hard limit is missing from the summary, change `checkpoint_instructions`. Do not set `num_history_runs` back to `None`.

## How you know the Web is right

Locally set the trigger to `7000`, `debug_mode=True`, `num_history_runs=100`.

| # | Do this | You must see |
| --- | --- | --- |
| 1 | Chat / tool until you cross the limit | Logs flip from `SKIP (x < 7000)` to one `TRIGGERED`; a folded card `ak → bk (-%)`, not another long bubble |
| 2 | Expand the card | Intent / Ground Truth have **exact numbers and paths**, not “looked up the weather” |
| 3 | Ask “what did I ask you to do first?” | The model still has turn-1 constraints; do not rely on a pruned raw bubble in the sidebar |
| 4 | Refresh | The checkpoint card is still there; `/frames` replay opens the same summary |
| 5 | Contrast with `num_history_runs=None` (local only) | From turn 4 early constraints vanish and you may `SKIP` forever — that is why **100** is required |
| 6 | Langfuse / OTel | A `context_compression` span with `original_tokens` / `saved_tokens` |

Seal separately: abort a tool mid-run; the next turn must not throw `Invalid message order`.
