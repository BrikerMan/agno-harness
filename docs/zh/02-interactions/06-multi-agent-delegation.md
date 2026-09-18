# 02-interactions / 多智能体协作与委托 (Multi-Agent Delegation)

在复杂业务流中，单一 Agent 难以承担所有任务。通常由一个主调度 Agent（Supervisor / Router）将子任务委托给具备专属工具和 Prompt 的专职子智能体（如 Researcher, Reviewer, Coder）。

`agno-harness` 提供了开箱即用的多智能体透传工具包 `SubAgentToolkit`，保证子 Agent 的流式输出无缝渲染，同时不污染父级上下文。

---

## 1. 注册与委托 (`SubAgentToolkit`)

```python
from agno.agent import Agent
from agno_harness import AgentRuntime, HideToolFilter, SubAgentToolkit

# 1. 定义专职子 Agent
researcher = Agent(name="Researcher", description="负责网络调研与论文检索", telemetry=False)
reviewer = Agent(name="Reviewer", description="负责代码质量与安全审计", telemetry=False)

# 2. 组装委托工具包
subagent_toolkit = SubAgentToolkit(agents=[researcher, reviewer])

# 3. 注入父 Agent 并注册工具过滤器
parent_agent = Agent(
    name="Coordinator",
    tools=[subagent_toolkit],
    instructions="协调各专家子 Agent 完成任务",
    telemetry=False,
)
runtime = AgentRuntime(agent=parent_agent)
runtime.register_tool_filter(HideToolFilter(subagent_toolkit.tool_names))
```

模型调用：`agent_name` + UI 用的短 `description`（3–6 词）+ 给子 agent 的完整 `prompt`。`stream=True`（默认）走 `substream` 出面板；`session_id` 可续跑同一子会话。每个子 agent 同样 `telemetry=False`。`SubAgentTool` 是 alias。

不要在 `substream` 周围并行打父散文——括号开着时父 chunk 被拦住，硬并行会把时序打乱。需要一次性委托再用手动 `substream`，不要无意义嵌套括号。

Agno session **不知道**委托。刷新要看到子面板，必须走 event log / `/frames`，同一套 `applyEvent`。前端：`subAgents` 有 `running` 时，后续 `TEXT_*` / `TOOL_*` / `REASONING_*` / `ui.*` 进 child body。见 [Web 03](../03-clients/01-web-react/03-stream-and-scroll.md)。

---

## 2. 线上多端事件流转协议

当主 Agent 决定委托任务给子 Agent 时，Wire 协议会产生清晰的子流嵌套协议：

| 事件类型 | 关键载荷 | 客户端呈现效果 |
| :--- | :--- | :--- |
| **`subagent.start`** | `agent_name`, `subRunId`, `description`, `prompt` | 前端开启一个可折叠的子任务工作台卡片 |
| **子 Agent 内部事件** | 包含子 Agent 内部的流式文本、工具调用等 | 实时流式输出在子任务工作台内部 |
| **`subagent.end`** | `subRunId` | 子任务工作台标记为完成，恢复主 Agent 思考 |

在子流执行期间，`EventSequencer` 会拦截父 Agent 的散乱 chunk，确保多智能体输出在时序上绝对连续、不出现交替混乱。
