# 05. 多智能体协作与委托（Multi-Agent Delegation）

> **⚠️ 明确架构边界与说明**：  
> `agno-harness` **目前明确不采用、也不支持 Agno 官方的 `Team` 抽象**。  
> Agno 原生 `Team` 内部采用递归黑盒调度，流式 Chunk 混合无序、丢弃中间思考与工具时序，极易导致前端渲染崩溃及上下文剧烈污染。  
> 
> `agno-harness` 沉淀了经过大规模生产验证的自研多智能体理念：**Supervisor-Delegate（主控-专家委托）架构**，配合 **`SubAgentToolkit`** 与 **`substream` 侧信道插桩**，完美解决多 Agent 实时流式与上下文隔离。

---

## 1. 为什么坚决淘汰 Agno 原生 Team？

| 痛点维度 | Agno 官方 Team 的致命缺陷 | agno-harness 自研委托架构 (`SubAgentToolkit`) |
| :--- | :--- | :--- |
| **流式透传** | **黑盒阻塞**：子 Agent 在内部运行期间，客户端只能干等转圈，中间 Token/思考全丢 | **实时插桩**：通过 `substream` 侧信道将子 Agent 的思考与打字机毫秒级透传至前端 |
| **协议边界** | **无时序帧**：所有专家的输出混在一锅粥里，客户端无法识别是谁在说话 | **严格定界**：精确发出 `subagent.start` 与 `subagent.end` 物理帧，前端优雅折叠展示 |
| **上下文管理** | **相互污染**：子 Agent 的杂乱中间轮次直接堆砌在主 Session 中，Token 迅速爆炸 | **物理隔离**：专家的思考过程仅流式展示，主 Agent 上下文仅保留最终精炼摘要 |
| **IM 防熔断** | **并发并发炸屏**：多专家若同时编辑消息，极易触发 Teams/飞书 `HTTP 429` 熔断 | **网关级汇聚**：在 IM 平台统一由 `RelayApp` 收集各专家交付物后一次性下发 |

---

## 2. 我们的多智能体设计哲学

```
                              用户指令
                                 │
                                 ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 主控 Coordinator Agent (大脑 / Supervisor)                             │
│ • 拥有全局系统认知与任务拆解能力                                        │
│ • 挂载专职 SubAgentToolkit                                              │
└────────────────────────────────────────────────────────────────────────┘
                                 │
                   调用工具: delegate_to_specialist()
                                 │
      ┌──────────────────────────┴──────────────────────────┐
      ▼                                                     ▼
┌──────────────────────────┐               ┌──────────────────────────┐
│ 论文检索专家 (Researcher) │               │ 代码安全审计专家 (Reviewer)│
│ • 专属 Prompt & 搜索工具  │               │ • 专属 Prompt & AST 规则 │
└──────────────────────────┘               └──────────────────────────┘
      │ (通过 substream 侧信道)                   │ (通过 substream 侧信道)
      ▼                                                     ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Wire 协议层物理定界:                                                   │
│   1. CUSTOM subagent.start { subRunId, agent_name: "Researcher" }       │
│   2. 实时流式透传子 Agent 的 reasoning, text 与 tool_calls              │
│   3. CUSTOM subagent.end { subRunId }                                  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 完整实战代码

新建文件 `multi_agent_delegation.py`：

```python
"""multi_agent_delegation.py — 基于 SubAgentToolkit 的生产级多智能体委托"""
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

# 1. 定义专职领域的专家子智能体 (Specialists)
# 每个 Specialist 拥有独立的提示词与专属工具，上下文与主 Agent 物理隔离
researcher = Agent(
    name="Researcher",
    description="负责全网最新技术趋势调研、学术论文检索与客观事实挖掘",
    model=model,
    instructions=["专注于通过搜索引擎检索客观事实与一手数据。回答务必精确，标明出处。"],
    tools=[DuckDuckGoTools()],
    telemetry=False,
)

security_reviewer = Agent(
    name="SecurityReviewer",
    description="负责代码安全性审计、OWASP 漏洞识别与合规架构审查",
    model=model,
    instructions=["专注于安全攻防、注入漏洞、数据泄露风险。使用严谨的黑客与合规视角。"],
    tools=[],
    telemetry=False,
)

# 2. 组装多智能体透传工具包 (SubAgentToolkit)
# 该工具包会自动为每个专家生成 delegate 工具，并在调用时自动挂载 substream 实时流
subagent_toolkit = SubAgentToolkit(agents=[researcher, security_reviewer])

# 3. 定义主控 Coordinator Agent
coordinator = Agent(
    name="Tech Lead Coordinator",
    model=model,
    instructions=[
        "你是一个大型研发团队的技术负责人（Tech Lead）。",
        "面对用户的技术咨询或方案评审：",
        "1. 首先调用 Researcher 调查最新的技术方案与产业动态；",
        "2. 随后调用 SecurityReviewer 审查该方案的潜在安全合规隐患；",
        "3. 最后综合两位专家的建议，给出权威落地的架构选型决策。",
    ],
    tools=[subagent_toolkit],
    markdown=True,
    telemetry=False,
)

# 4. 驱动核心执行引擎，并注册工具过滤器（隐藏内部委托细节）
runtime = AgentRuntime(
    agent=coordinator,
    enable_subagent_streaming=True, # 开启子智能体流式插桩总线
)
# 隐藏委托工具的底层原始函数名，保持用户界面清爽
runtime.register_tool_filter(HideToolFilter(subagent_toolkit.tool_names))

# 5. 挂载到全渠道网关 RelayApp
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
            print("\n🚀 生产级多智能体委托工作台已就绪！在终端输入你的方案（如：'评估微服务引入 gRPC 的利弊'）：\n")
            await cli_channel.run_interactive_loop(
                chat_id="multi-agent-session",
                sender_id="architect",
            )
        finally:
            await relay.stop()

    asyncio.run(main())
```

---

## 4. 跨渠道与前端交互体验

### A. Web React 端呈现（AG-UI 标准）
- 当 Coordinator 委托 `Researcher` 时，前端立即收到 `subagent.start` 事件；
- React 前端会渲染一个专属的 **可折叠子任务工作台（Sub-Task Drawer / Accordion）**；
- 用户的目光可以在主界面看全局结论，也可以随时展开工作台查看 `Researcher` 正在实时调用 DuckDuckGo 搜索以及每一句的思考流式输出；
- 完成后收到 `subagent.end`，抽屉打上绿色已完成勾标。

### B. 终端 CLI 体验
- 终端打印专属的 `[Sub-Agent: Researcher]` 彩色分块徽章；
- 专家的流式输出平滑渐进渲染，完成后优雅缩进归并到主线。

### C. 飞书与 Teams 体验
- 避免了群聊被刷屏几十条的灾难；
- `RelayApp` 在 IM 端保障主控 Coordinator 最终汇聚产出，输出结构分明的富文本或自适应卡片。

协议与隔离细节：[交互 06 多 Agent 委托](../02-interactions/06-multi-agent-delegation.md)。
