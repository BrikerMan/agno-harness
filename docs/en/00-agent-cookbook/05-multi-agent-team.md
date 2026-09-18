# 05. Multi-Agent Delegation

> **Architecture boundary**:
> `agno-harness` **does not use or support Agno's official `Team` abstraction**.
> Native Agno `Team` uses recursive black-box scheduling. Stream chunks mix out of order, and intermediate reasoning and tool timing are dropped. That often breaks frontend rendering and pollutes context.
>
> `agno-harness` uses a production **Supervisor-Delegate** design with **`SubAgentToolkit`** and a **`substream` side channel**, so multi-agent runs stay streamed and context-isolated.

---

## 1. Why not Agno native Team?

| Pain point | Agno official Team | agno-harness delegation (`SubAgentToolkit`) |
| :--- | :--- | :--- |
| **Streaming** | **Black-box blocking**: while a child agent runs, the client waits. Intermediate tokens and reasoning are dropped | **Live instrumentation**: the `substream` side channel forwards the child's reasoning and typewriter output to the frontend in milliseconds |
| **Protocol boundaries** | **No timing frames**: every specialist's output is mixed together, so the client cannot tell who is speaking | **Strict delimiters**: `subagent.start` and `subagent.end` frames let the frontend fold each specialist cleanly |
| **Context** | **Cross-contamination**: messy intermediate turns land in the main session and tokens explode | **Physical isolation**: the specialist's reasoning is streamed for display; the main agent keeps only a refined summary |
| **IM rate limits** | **Concurrent spam**: several specialists editing the same message can trip Teams/Lark `HTTP 429` | **Gateway aggregation**: `RelayApp` collects each specialist's deliverable and sends one IM message |

---

## 2. Multi-agent design

```
                              User instruction
                                 │
                                 ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Coordinator Agent (supervisor)                                         │
│ • Global task breakdown and routing                                    │
│ • Mounts SubAgentToolkit                                               │
└────────────────────────────────────────────────────────────────────────┘
                                 │
                   Tool call: delegate_to_specialist()
                                 │
      ┌──────────────────────────┴──────────────────────────┐
      ▼                                                     ▼
┌──────────────────────────┐               ┌──────────────────────────┐
│ Researcher               │               │ Security reviewer        │
│ • Own prompt & search    │               │ • Own prompt & AST rules │
└──────────────────────────┘               └──────────────────────────┘
      │ (via substream side channel)              │ (via substream side channel)
      ▼                                                     ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Wire-protocol delimiters:                                              │
│   1. CUSTOM subagent.start { subRunId, agent_name: "Researcher" }       │
│   2. Live stream of the sub-agent's reasoning, text, and tool_calls    │
│   3. CUSTOM subagent.end { subRunId }                                  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Full example

Create `multi_agent_delegation.py`:

```python
"""multi_agent_delegation.py — production multi-agent delegation with SubAgentToolkit"""
import asyncio
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools.duckduckgo import DuckDuckGoTools
from agno_harness import (
    AgentRuntime,
    CLIChannel,
    HideToolFilter,
    LarkChannel,
    RelayApp,
    RelayConfig,
    SubAgentToolkit,
    TeamsChannel,
    WebChannel,
    setup_relay_logging,
)

setup_relay_logging(level="INFO")

model = OpenAIChat(
    id=RelayConfig.llm_model(default="gpt-4o"),
    base_url=RelayConfig.llm_base_url() or None,
    api_key=RelayConfig.llm_api_key() or None,
)

# 1. Domain specialists
# Each specialist has its own prompt and tools. Context is isolated from the coordinator.
researcher = Agent(
    name="Researcher",
    description="Surveys technical trends, academic papers, and factual sources",
    model=model,
    instructions=["Search for facts and primary sources. Be precise and cite sources."],
    tools=[DuckDuckGoTools()],
    telemetry=False,
)

security_reviewer = Agent(
    name="SecurityReviewer",
    description="Audits code security, OWASP issues, and compliance architecture",
    model=model,
    instructions=["Focus on offensive and defensive security, injection, and data-leak risk. Use a rigorous security and compliance lens."],
    tools=[],
    telemetry=False,
)

# 2. Assemble SubAgentToolkit
# The toolkit generates a delegate tool per specialist and mounts substream on each call.
subagent_toolkit = SubAgentToolkit(agents=[researcher, security_reviewer])

# 3. Coordinator Agent
coordinator = Agent(
    name="Tech Lead Coordinator",
    model=model,
    instructions=[
        "You are the tech lead of a large engineering team.",
        "For a technical question or design review:",
        "1. Call Researcher first for current approaches and industry context;",
        "2. Then call SecurityReviewer for security and compliance risks;",
        "3. Combine both reports into a concrete architecture decision.",
    ],
    tools=[subagent_toolkit],
    markdown=True,
    telemetry=False,
)

# 4. Runtime plus a tool filter that hides internal delegate names
runtime = AgentRuntime(
    agent=coordinator,
    enable_subagent_streaming=True,  # sub-agent streaming bus
)
# Hide raw delegate function names so the UI stays clean
runtime.register_tool_filter(HideToolFilter(subagent_toolkit.tool_names))

# 5. Mount on RelayApp
relay = RelayApp(runtime=runtime, enable_deduplication=True)

relay.add_channel(WebChannel())
cli_channel = CLIChannel()
relay.add_channel(cli_channel)

if RelayConfig.lark_app_id():
    relay.add_channel(LarkChannel(use_websocket=True))

if RelayConfig.teams_app_id():
    relay.add_channel(TeamsChannel())


if __name__ == "__main__":
    async def main():
        await relay.start()
        try:
            print("\nMulti-agent delegation is ready. Type a design question (for example: 'Evaluate gRPC for microservices'):\n")
            await cli_channel.run_interactive_loop(
                chat_id="multi-agent-session",
                sender_id="architect",
            )
        finally:
            await relay.stop()

    asyncio.run(main())
```

---

## 4. Cross-channel frontend experience

### A. Web React (AG-UI)

- When the coordinator delegates to `Researcher`, the frontend receives `subagent.start` immediately.
- React renders a dedicated **collapsible sub-task drawer / accordion**.
- The user can watch the coordinator's conclusion on the main surface, or expand the drawer to see `Researcher` call DuckDuckGo and stream each reasoning token.
- On `subagent.end`, the drawer shows a green completed check.

### B. Terminal CLI

- The terminal prints a colored `[Sub-Agent: Researcher]` badge.
- The specialist's stream renders incrementally, then indents back into the main line.

### C. Lark and Teams

- The group chat is not flooded with dozens of messages.
- `RelayApp` waits for the coordinator to gather the deliverables, then sends one structured rich-text or Adaptive Card.

Protocol and isolation: [interaction 06 multi-agent delegation](../02-interactions/06-multi-agent-delegation.md).
