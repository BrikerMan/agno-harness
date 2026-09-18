# 02-interactions / Todos

Multi-step work needs its own progress surface, not another chat bubble. Long documents live in [08 Artifacts](../08-streaming-artifacts.md).

On `todo_write` the harness cleans the Markdown and emits `ui.block` / `ui.item`. The client consumes structured blocks; it does not parse half-finished JSON.

| Step | Contents |
| --- | --- |
| [01 Toolkit](01-toolkit.md) | `TodoToolkit`, `allow_subtasks`, states, IM checklists |
| [02 Sidebar UI](02-sidebar-ui.md) | Decoupled from chat, two layouts, anti-spin, hierarchical index, **how you know the Web is right** |
| [03 Replay and FAQ](03-replay-and-faq.md) | Same `applyEvent`, mid-stop, FAQ |
