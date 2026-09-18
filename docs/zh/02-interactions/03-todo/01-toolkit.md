# 01. TodoToolkit

不要让前端硬解 `todo_write` 的 `TOOL_CALL_ARGS`。Harness 执行工具时：

```text
模型 → todo_write(todos="- [x] 1. 准备\n- [-] 2. 执行...")
     → parse_todo_list() → level=0/1 树
     → ui.block.start (schema="todo-list")
     → ui.item × N
     → ui.block.end
```

`todo-list` 是**替换型**块。前端 `REPLACING_SCHEMAS` 原地换掉上一张，禁止每轮往下刷一屏。

## 接入

默认 `add_instructions=True`，Agno 把对应 Prompt 并进 `_tool_instructions`，主 instructions 只写业务。

```python
from agno.agent import Agent
from agno_harness import AgentRuntime, HideToolFilter, TodoToolkit

todo_tool = TodoToolkit(default_title="任务执行规划")
# 两层树：TodoToolkit(default_title="...", allow_subtasks=True)

agent = Agent(name="workflow-agent", tools=[todo_tool], instructions="...")
runtime = AgentRuntime(agent=agent, catalog=catalog)
runtime.register_tool_filter(HideToolFilter({"todo_write"}))
```

- `allow_subtasks=False`：扁平 `- [ ]`，禁止缩进；模型偶发缩进会被展平。
- `allow_subtasks=True`：父行 `- [ ]`，子行两个空格。
- 两种 Prompt 都写死：**输出最终结论前必须再调一次 `todo_write`，全部 `[x]`，禁止残留 `[-]`。**

`todo_tool.instructions` / `TodoToolkit.instructions(allow_subtasks=False)` 都能取出当前文案。工具 description 随开关变。

## 状态

| 状态 | Markdown | UI |
| --- | --- | --- |
| `pending` | `- [ ]` | 排队 |
| `doing` | `- [-]` | Spinner + 高亮 |
| `done` | `- [x]` | 打勾划线 |
| `error` | `- [e]` | 失败 |

IM（Teams / 飞书）把同一棵树编成原生勾选列表，不要每步一条气泡。

## 扩展字段

继承 `ItemSchema` / `TodoToolkit`，在 `todo_write` 里补 `assignee` 等，再 `ui_block` + `emit_item`。解析继续用 `parse_todo_list`。

下一步：[02 侧栏 UI](02-sidebar-ui.md)。
