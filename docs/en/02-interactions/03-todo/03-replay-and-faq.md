# 03. Replay and FAQ

`ui.block` / `ui.item` land in the event log. Refresh, `/frames`, another device: replay with the same `applyEvent`. `activeTodoBlock` is the checks as they were. Do not re-parse Markdown.

On interrupt (error, abort) the store holds the last `ui.item`. Replay stays on `doing` / `error`. Do not “recompute” the list in the client and erase the scene.

Live and replay share one pipeline: [Web 01 iron rules](../../03-clients/01-web-react/01-iron-rules.md).

## FAQ

1. **A few seconds of freeze after the answer?**  
   Check whether `make_thread_title_hook` is blocking this SSE. In production, title generation should be async, or show a first-line truncate on turn one.

2. **Bar stuck at 80%–90% spinning?**  
   The model skipped the last `todo_write`. Confirm toolkit instructions are mounted and the client has the last-step close from [02](02-sidebar-ui.md).

3. **A raw `todo_write` argument card in the bubble?**  
   Panel tools do not render as tool cards. `HideToolFilter({"todo_write"})`, or return `null` for that name in `BodyView`.

4. **The third parent became `#4`?**  
   Do not use the array index. Use `parentNum` / `subNum` from [02](02-sidebar-ui.md).

The six-step sidebar walkthrough is [02 §5](02-sidebar-ui.md). Replay must share `applyEvent` with live. Do not recompute a stopped scene in the client.
