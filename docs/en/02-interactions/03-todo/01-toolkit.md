# 01. TodoToolkit

Do not make the client parse `todo_write` `TOOL_CALL_ARGS`. When the harness runs the tool:

```text
model → todo_write(todos="- [x] 1. Prep\n- [-] 2. Run...")
     → parse_todo_list() → level=0/1 tree
     → ui.block.start (schema="todo-list")
     → ui.item × N
     → ui.block.end
```

`todo-list` is a **replacing** block. Put it in `REPLACING_SCHEMAS` and swap in place. Do not append a new list every turn.

## Wiring

Default `add_instructions=True`. Agno merges the matching prompt into `_tool_instructions`. Keep the main instructions on the business.

```python
from agno.agent import Agent
from agno_harness import AgentRuntime, HideToolFilter, TodoToolkit

todo_tool = TodoToolkit(default_title="Execution plan")
# two-level tree: TodoToolkit(default_title="...", allow_subtasks=True)

agent = Agent(name="workflow-agent", tools=[todo_tool], instructions="...")
runtime = AgentRuntime(agent=agent, catalog=catalog)
runtime.register_tool_filter(HideToolFilter({"todo_write"}))
```

- `allow_subtasks=False`: flat `- [ ]`, no indent; accidental indent is flattened.
- `allow_subtasks=True`: parent `- [ ]`, child two spaces.
- Both prompts hard-require: **call `todo_write` once more before the final answer, all `[x]`, no leftover `[-]`.**

`todo_tool.instructions` / `TodoToolkit.instructions(allow_subtasks=False)` return the current copy. The tool description follows the flag.

## States

| State | Markdown | UI |
| --- | --- | --- |
| `pending` | `- [ ]` | queued |
| `doing` | `- [-]` | spinner + highlight |
| `done` | `- [x]` | check + strike |
| `error` | `- [e]` | failed |

On Teams / Lark, compile the same tree into a native checklist. Do not send one bubble per step.

## Extra fields

Subclass `ItemSchema` / `TodoToolkit`, add `assignee` (etc.) inside `todo_write`, then `ui_block` + `emit_item`. Keep parsing on `parse_todo_list`.

Next: [02 Sidebar UI](02-sidebar-ui.md).
