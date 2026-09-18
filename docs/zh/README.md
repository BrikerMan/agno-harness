# agno-harness 文档

[English](../en/README.md) | 简体中文（正文）

`agno-harness` 是企业级 Agent 脚手架：**Agno Runtime + Relay**。你写业务和卡片；刷新续写、智能压缩、多渠道投递已经在底座里。

文档分三层：先照着菜谱跑通，再查 SDK，最后才看线级协议。

---

## 1. 第一层：菜谱

从本地终端到 Web、Teams、飞书。

1. [01 纯 CLI](00-agent-cookbook/01-pure-cli-agent.md)
2. [02 Web / FastAPI](00-agent-cookbook/02-web-fastapi-agent.md)
3. [03 Teams](00-agent-cookbook/03-teams-bot-agent.md)
4. [04 飞书 / Lark](00-agent-cookbook/04-lark-feishu-agent.md)
5. [05 多 Agent 委托](00-agent-cookbook/05-multi-agent-team.md)
6. [06 附件、插话、日志、审计](00-agent-cookbook/06-advanced-fancy-modules.md)

菜谱目录：[00-agent-cookbook/README.md](00-agent-cookbook/README.md)

---

## 2. 第二层：SDK

### 2.1 底座

1. [01 架构与分层](01-foundations/01-architecture.md)
2. [02 FastAPI 双模接入](01-foundations/02-fastapi-integration.md)
3. [03 会话拓扑](01-foundations/03-sessions-and-topology.md)
4. [04 可观测性](01-foundations/04-observability.md)
5. [05 持久化与长任务](01-foundations/05-persistence-and-longruns/README.md)
6. [06 测试与验收](01-foundations/06-testing-and-verification.md)
7. [07 写一个 Agent](01-foundations/07-writing-an-agent.md)

### 2.2 交互

1. [01 Class-First 卡片](02-interactions/01-class-first-cards.md)
2. [02 等人确认与卡片动作](02-interactions/02-hitl-and-actions/README.md)
3. [03 Todo](02-interactions/03-todo/README.md)
4. [04 压缩与封口](02-interactions/04-compression-and-sealing/README.md)
5. [05 发给模型的用户请求](02-interactions/05-user-query.md)
6. [06 多 Agent 委托](02-interactions/06-multi-agent-delegation.md)
7. [07 Skills 与 JIT](02-interactions/07-skills-and-jit.md)
8. [08 流式 Artifact](02-interactions/08-streaming-artifacts.md)

### 2.3 渠道

1. [01 Web / React](03-clients/01-web-react/README.md) — 复制 [frontend-kit](../../resources/frontend-kit/README_zh.md) · [01 铁律](03-clients/01-web-react/01-iron-rules.md) · [02 外壳](03-clients/01-web-react/02-thread-shell.md) · [03 事件流与跟滚](03-clients/01-web-react/03-stream-and-scroll.md) · [04 刷新续写](03-clients/01-web-react/04-attach-and-longrun.md) · [05 HITL / 卡片 / DevTools](03-clients/01-web-react/05-hitl-cards-devtools.md)
2. [02 CLI](03-clients/02-cli-terminal.md)
3. [03 Teams](03-clients/03-teams-adapter.md)
4. [04 飞书 / Lark](03-clients/04-lark-adapter.md)

---

## 3. 第三层：线级与避坑

1. [01 前端零改动接入](04-deep-dive-and-faq/01-react-zero-code-integration.md)
2. [02 AG-UI 线级协议](04-deep-dive-and-faq/02-protocol-wire-spec.md)
3. [03 生产避坑](04-deep-dive-and-faq/03-production-pitfalls-and-faq.md)

---

## 4. 规范

1. [SPEC.md](../../SPEC.md) — 协议与架构规约
2. [AGENTS.md](../../AGENTS.md) — 分层与编码红线
