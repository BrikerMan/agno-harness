# 多 Agent

何时读：父 agent 要委托，且用户要看着子 agent 干活。

## SubAgentToolkit（首选）

子 agent 注册一次。父 agent 只有一个工具 `delegate_subagent`；花名册来自各 `Agent.description`。

```python
from agno_relay import HideToolFilter, SubAgentToolkit

toolkit = SubAgentToolkit(agents=[reviewer, researcher])
parent = Agent(model=..., tools=[toolkit, ...], instructions=BASE, telemetry=False)
runtime = AguiRuntime(agent=parent, db=db)
runtime.register_tool_filter(HideToolFilter(toolkit.tool_names))
```

模型调用：`agent_name` + UI 用的短 `description`（3–6 词）+ 给子 agent 的完整 `prompt`。`stream=True`（默认）走 `substream` 出面板；`session_id` 可续跑。

`SubAgentTool` 是 alias。每个子 agent 同样 `telemetry=False`。

## 线上形态

| 事件 | 内容 |
| --- | --- |
| `subagent.start` | 名字、`subRunId`、`description`、`prompt`、可选 `toolCallId` |
| 普通 AG-UI 帧 | 子 agent 自己的流 |
| `subagent.end` | `subRunId` |

括号开着时父 chunk 被拦住，保证子流连续。不要试图在 `substream` 周围并行打父散文。

Agno session 不知道委托。刷新要看到面板，必须走 [event log / frames](persistence.md)，同一套 reducer。前端：`subAgents` 有 `running` 时，后续 `TEXT_*` / `TOOL_*` / `REASONING_*` / `ui.*` 进 child body。

需要一次性委托再用手动 `substream`。不要无意义嵌套括号。
