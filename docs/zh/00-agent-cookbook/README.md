# 菜谱

先 Runtime，再 Relay。按这个顺序跑。

1. **[01 CLI](01-pure-cli-agent.md)** — 本机终端，不用凭证。
2. **[02 Web / FastAPI](02-web-fastapi-agent.md)** — `AgentRuntime` + AG-UI。产品壳五步：[Web / React](../03-clients/01-web-react/README.md)。
3. **[03 Teams](03-teams-bot-agent.md)** — `agno-harness teams onboard`，群里一张卡，不刷屏。
4. **[04 飞书](04-lark-feishu-agent.md)** — 扫码创建应用，WebSocket，不用公网 IP。
5. **[05 帮手](05-multi-agent-team.md)** — `SubAgentToolkit`：帮手自己一块面板，主 Agent 只拿结论。
6. **[06 扩展](06-advanced-fancy-modules.md)** — 附件、插话策略、日志、审计库。
7. **[07 Markdown 笔记](07-teams-knowledge-bot.md)** — 每个 Agent 有自己的 `knowledge/*.md`。改完文件，下次搜索就能读到。
