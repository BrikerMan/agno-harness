# 03-clients / Web / React

从没见过这套协议也能做出产品壳。协议保证什么、产品必须自己做，分开写。

**参考客户端：** 复制 [`frontend-kit/`](../../../../resources/frontend-kit/README_zh.md)（`applyEvent` + `useAguiChat`）。不要重写 reducer。侧栏、Composer、卡片仍是产品自己的。

**刷新续写 / SSE keepalive：** 先读 [04 attach](04-attach-and-longrun.md)。存储层 → [持久化](../../01-foundations/05-persistence-and-longruns/README.md)。HITL → [web-resume](../../02-interactions/02-hitl-and-actions/03-web-resume.md)。Todo 侧栏 → [03-todo](../../02-interactions/03-todo/README.md)。卡片 → [01 Class-First](../../02-interactions/01-class-first-cards.md)。

零改动接入与 DevTools：[第三层 01](../../04-deep-dive-and-faq/01-react-zero-code-integration.md)。

| 步 | 内容 |
| --- | --- |
| [01 铁律](01-iron-rules.md) | 五条产品红线 |
| [02 外壳](02-thread-shell.md) | 标题、四态侧栏、未读、跨页 HITL 唤回、Composer |
| [03 事件流与跟滚](03-stream-and-scroll.md) | `applyEvent`、`body.order[]`、stick-to-bottom |
| [04 刷新续写](04-attach-and-longrun.md) | `/attach`、cursor、ping、idle、long-run |
| [05 收口](05-hitl-cards-devtools.md) | HITL / 卡片 / DevTools / 验收 |
