# 03-clients / 飞书与 Lark 适配指南 (Lark Adapter)

飞书（Lark）在国内及出海跨国企业中使用广泛。飞书提供了丰富的交互式卡片 v2、长连接免公网调试、群聊表情互动等能力。

---

## 1. 快速接入

```python
from agno_harness import RelayApp
from agno_harness.channels.lark import LarkChannel

lark = LarkChannel(
    app_id="cli_a1b2c3d4e5",
    app_secret="your-lark-app-secret",
    verification_token="your-verification-token",
    encrypt_key="your-encrypt-key", # 可选
)

relay = RelayApp(agent).add_channel(lark)
```

---

## 2. 免公网长连接模式 (WebSocket Mode)

对于内网部署、本地开发或没有固定公网 IP 的企业私有服务器，不需要申请公网域名或配置内网穿透（ngrok）：
- 开启飞书开放平台长连接机制；
- `LarkChannel` 通过标准 WebSocket 与飞书网关建立安全长连，免去复杂的 Webhook 证书配置与网络暴露风险。

---

## 3. Webhook 幂等去重防雪崩 (Idempotency)

飞书的事件推送要求开发者服务器在 3 秒内返回 200。当 Agent 推理复杂耗时超过 3 秒时，飞书服务器会自动重试重发该事件：
- **雪崩风险**：如果服务不加去重，一次提问会触发 3~5 次重复的 Agent 推理，导致群里机器人连珠炮式重复回答，Token 消耗翻数倍；
- **自动防线**：`RelayApp` 内置 `enable_deduplication=True`，基于 `event_id` / `message_id` 进行内存窗口拦截，重复事件自动丢弃。
