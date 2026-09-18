# Cookbooks

Runtime first, then Relay. Run them in this order.

1. **[01 CLI](01-pure-cli-agent.md)** — local terminal, no credentials.
2. **[02 Web / FastAPI](02-web-fastapi-agent.md)** — `AgentRuntime` + AG-UI. Product shell: [Web / React](../03-clients/01-web-react/README.md).
3. **[03 Teams](03-teams-bot-agent.md)** — `agno-harness teams onboard`; one card in the group, no spam.
4. **[04 Lark](04-lark-feishu-agent.md)** — scan to create the app; WebSocket, no public IP.
5. **[05 Helpers](05-multi-agent-team.md)** — `SubAgentToolkit`: the helper gets its own panel; the main agent only sees the conclusion.
6. **[06 Extensions](06-advanced-fancy-modules.md)** — attachments, chime-in policy, logging, audit store.
