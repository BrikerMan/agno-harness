# 03-clients / Microsoft Teams 适配指南 (Teams Adapter)

Microsoft Teams 是微软体系下的企业通讯中枢。然而由于其严格的 API 限流、复杂的 Bot Framework 协议以及不同于 Web 的 Adaptive Cards 规范，直接接入极易踩坑。

`agno-harness` 吸收了一线生产实战经验，提供了坚固的 `TeamsChannel`。

---

## 1. 快速接入

```python
from agno_harness import RelayApp
from agno_harness.channels.teams import TeamsChannel

teams = TeamsChannel(
    bot_app_id="your-azure-bot-app-id",
    bot_app_password="your-azure-bot-secret",
    bot_tenant_id="your-tenant-id",
)

relay = RelayApp(agent).add_channel(teams)
```

将路由挂载进 FastAPI：
```python
app.include_router(relay.get_router(resolve_user_id=...))
```
此时 Teams Bot Webhook 会自动在 `POST /api/messages` 监听。

---

## 2. 流模式避坑：为什么严禁 RAW 模式？

- **Teams Webhook 限流机制**：Teams Graph API 与 Bot Framework 对同一会话有严格的更新速率限制。如果以 `StreamMode.RAW` 每生成一个 Token 就更新一次卡片，不出 3 秒就会收到 `HTTP 429 Too Many Requests`，机器人直接被微软限流禁言数小时；
- **推荐方案**：
  - **默认模式 (`stream_mode="final"`)**：等待 Agent 思考并生成完毕后，一次性将正文与聚合的 Adaptive Card 发送给用户；
  - **节流模式 (`stream_mode="throttle"`)**：如果业务必须看到流式打字效果，必须使用 `ThrottledStreamBuffer`，以不低于 1.5 秒的时间窗口批量编辑更新消息。

---

## 3. Reaction 状态流转保障

当 Teams 用户 @ 机器人提问时：
1. **即时 Reaction ACK**：网关在毫秒级内向用户消息打上 `👀` 表情（Reaction），同时向频道发送 `Typing` 指示器；
2. **正常完成**：Agent 回复完成后，`settle()` 自动将 Reaction 切换为 `✅`；
3. **异常报错**：若发生报错或超时，`try-finally` 兜底将表情切换为 `❌`，并给出清晰提示，避免群聊用户陷入无休止等待。
