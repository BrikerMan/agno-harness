# 02-interactions / Compression and sealing

Long chats blow the token budget. Agno’s built-in compressor also stalls the stream and misses the KV cache. `SmartCompressionManager` takes over memory with one in-context checkpoint; `seal_session_run` closes dangling tool calls.

Assembly overview: [07 Writing an agent](../../01-foundations/07-writing-an-agent.md).

| Step | Contents |
| --- | --- |
| [01 Why + `num_history_runs`](01-why-and-num-history.md) | Native gaps; `0` / `None` / `100` |
| [02 Checkpoint and config](02-checkpoint-and-config.md) | Threshold matrix, checkpoint, user role, wiring |
| [03 Seal and FAQ](03-seal-and-faq.md) | `seal_session_run`; SKIP / None FAQ; **how you know the Web is right** |
