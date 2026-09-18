# 02-interactions / Multi-agent collaboration and delegation (Multi-Agent Delegation)

A single agent cannot own every step of a complex flow. A supervisor / router usually delegates subtasks to specialists (Researcher, Reviewer, Coder) that have their own tools and prompts.

`agno-harness` ships `SubAgentToolkit` so a child agent's stream renders without polluting the parent context.

---

## 1. Register and delegate (`SubAgentToolkit`)

```python
from agno.agent import Agent
from agno_harness import AgentRuntime, HideToolFilter, SubAgentToolkit

# 1. Specialist children
researcher = Agent(name="Researcher", description="Web research and paper lookup", telemetry=False)
reviewer = Agent(name="Reviewer", description="Code quality and security review", telemetry=False)

# 2. Delegation toolkit
subagent_toolkit = SubAgentToolkit(agents=[researcher, reviewer])

# 3. Parent agent plus a filter that hides the raw delegate tool names
parent_agent = Agent(
    name="Coordinator",
    tools=[subagent_toolkit],
    instructions="Coordinate specialist agents to finish the task",
    telemetry=False,
)
runtime = AgentRuntime(agent=parent_agent)
runtime.register_tool_filter(HideToolFilter(subagent_toolkit.tool_names))
```

The model calls `agent_name` + a short UI `description` (3–6 words) + the full `prompt` for the child. `stream=True` (default) opens a `substream` panel; `session_id` resumes the same child session. Every child also needs `telemetry=False`. `SubAgentTool` is an alias.

Do not emit parent prose in parallel around a `substream` — parent chunks are held while the bracket is open; forcing them interleaves the timeline. Use a manual `substream` only for one-shot delegation. Do not nest brackets for no reason.

The Agno session **does not** know about delegation. A refresh that must show the child panel needs the event log / `/frames` and the same `applyEvent`. On the client, while a child is `running`, later `TEXT_*` / `TOOL_*` / `REASONING_*` / `ui.*` go into the child body. See [Web 03](../03-clients/01-web-react/03-stream-and-scroll.md).

---

## 2. Multi-channel event flow

When the parent delegates to a child, the wire protocol nests a clear substream:

| Event | Key payload | Client effect |
| :--- | :--- | :--- |
| **`subagent.start`** | `agent_name`, `subRunId`, `description`, `prompt` | The frontend opens a foldable specialist workbench card |
| **Child-agent events** | The child's streamed text, tool calls, and so on | Live stream inside that workbench |
| **`subagent.end`** | `subRunId` | The workbench is marked done; the parent resumes |

While the child stream is running, `EventSequencer` holds back scattered parent chunks so multi-agent output stays in order and does not interleave.
