# 03-clients / Lark / Feishu adapter (Lark Adapter)

Lark (Feishu) is widely used in China and in overseas teams. It offers Interactive Card v2, a long-connection mode that needs no public IP, and group reaction UX.

---

## 1. Quick start

```python
from agno_harness import AgentRuntime, RelayApp
from agno_harness.channels.lark import LarkChannel

lark = LarkChannel(
    app_id="cli_a1b2c3d4e5",
    app_secret="your-lark-app-secret",
    verification_token="your-verification-token",
    encrypt_key="your-encrypt-key", # optional
)

relay = RelayApp(AgentRuntime(agent=agent)).add_channel(lark)
```

---

## 2. Long connection without a public IP (WebSocket Mode)

For intranet deploys, local development, or private servers without a static public IP, you do not need a public domain or a tunnel (ngrok):

- Turn on the Lark Open Platform long-connection mechanism
- `LarkChannel` opens a standard WebSocket to the Lark gateway — no webhook certificates and no network exposure

---

## 3. Webhook idempotency (retry storms)

Lark event delivery requires a 200 within 3 seconds. When agent reasoning takes longer than that, Lark retries the same event:

- **Storm risk**: without dedupe, one question starts 3–5 agent runs, the bot answers in a burst, and token spend multiplies
- **Built-in guard**: `RelayApp` sets `enable_deduplication=True` and drops duplicates in a memory window keyed by `event_id` / `message_id`
