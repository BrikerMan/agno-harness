# agno-harness docs

English | [简体中文](../zh/README.md)

`agno-harness` is enterprise agent scaffolding: **Agno Runtime + Relay**. You write the business and the cards. Resume, smart compression, and multi-channel delivery live in the base.

Docs are three layers: cookbooks first, then the SDK, then the wire protocol.

---

## 1. Layer 1: Cookbooks

Local terminal, then Web, Teams, and Lark.

1. [01 Pure CLI](00-agent-cookbook/01-pure-cli-agent.md)
2. [02 Web / FastAPI](00-agent-cookbook/02-web-fastapi-agent.md)
3. [03 Teams](00-agent-cookbook/03-teams-bot-agent.md)
4. [04 Lark / Feishu](00-agent-cookbook/04-lark-feishu-agent.md)
5. [05 Multi-agent delegation](00-agent-cookbook/05-multi-agent-team.md)
6. [06 Attachments, chime-in, logs, audit](00-agent-cookbook/06-advanced-fancy-modules.md)

Cookbook index: [00-agent-cookbook/README.md](00-agent-cookbook/README.md)

---

## 2. Layer 2: SDK

### 2.1 Foundations

1. [01 Architecture and layering](01-foundations/01-architecture.md)
2. [02 FastAPI dual-mode integration](01-foundations/02-fastapi-integration.md)
3. [03 Session topology](01-foundations/03-sessions-and-topology.md)
4. [04 Observability](01-foundations/04-observability.md)
5. [05 Persistence and long runs](01-foundations/05-persistence-and-longruns/README.md)
6. [06 Testing and verification](01-foundations/06-testing-and-verification.md)
7. [07 Writing an agent](01-foundations/07-writing-an-agent.md)

### 2.2 Interactions

1. [01 Class-First cards](02-interactions/01-class-first-cards.md)
2. [02 Wait-for-human and card actions](02-interactions/02-hitl-and-actions/README.md)
3. [03 Todos](02-interactions/03-todo/README.md)
4. [04 Compression and sealing](02-interactions/04-compression-and-sealing/README.md)
5. [05 The user request the model sees](02-interactions/05-user-query.md)
6. [06 Multi-agent delegation](02-interactions/06-multi-agent-delegation.md)
7. [07 Skills and JIT](02-interactions/07-skills-and-jit.md)
8. [08 Streaming artifacts](02-interactions/08-streaming-artifacts.md)

### 2.3 Channels

1. [01 Web / React](03-clients/01-web-react/README.md) — copy [frontend-kit](../../resources/frontend-kit/README.md) · [01 Iron rules](03-clients/01-web-react/01-iron-rules.md) · [02 Shell](03-clients/01-web-react/02-thread-shell.md) · [03 Stream and scroll](03-clients/01-web-react/03-stream-and-scroll.md) · [04 Attach and long-run](03-clients/01-web-react/04-attach-and-longrun.md) · [05 HITL / cards / DevTools](03-clients/01-web-react/05-hitl-cards-devtools.md)
2. [02 CLI](03-clients/02-cli-terminal.md)
3. [03 Teams](03-clients/03-teams-adapter.md)
4. [04 Lark / Feishu](03-clients/04-lark-adapter.md)

---

## 3. Layer 3: Wire spec and pitfalls

1. [01 Zero-change frontend integration](04-deep-dive-and-faq/01-react-zero-code-integration.md)
2. [02 AG-UI wire protocol](04-deep-dive-and-faq/02-protocol-wire-spec.md)
3. [03 Production pitfalls](04-deep-dive-and-faq/03-production-pitfalls-and-faq.md)

---

## 4. Specs

1. [SPEC.md](../../SPEC.md) — protocol and architecture
2. [AGENTS.md](../../AGENTS.md) — layering and coding guardrails
