# 01-foundations / 持久化与长任务

Agno session 是给**下一轮模型**的，不是用户看过的流。刷新还要看见卡片 / 子面板，或 run 必须活过那条 HTTP，用两套存储 + `LongRunManager`。

前端 idle 重连 → [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md)。本系列只写路由与存储职责。

| 步 | 内容 |
| --- | --- |
| [01 双存储](01-dual-storage.md) | 热 log vs archive；mixin；`X-Agui-Resume` |
| [02 长任务路由](02-longrun-routes.md) | `LongRunManager`、`/attach`、`/active`、`/abort`；ping vs Redis beat |
| [03 FAQ](03-faq.md) | 提问消失、空白气泡、Stop、`runCount` |
