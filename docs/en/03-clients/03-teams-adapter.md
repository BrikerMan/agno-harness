# 03-clients / Microsoft Teams adapter (Teams Adapter)

Microsoft Teams is the comms hub in Microsoft shops. Strict API rate limits, the Bot Framework protocol, and Adaptive Cards that differ from the web make a naive integration easy to get wrong.

`agno-harness` draws on production experience and ships a hardened `TeamsChannel`.

---

## 1. Quick start

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

Mount the router on FastAPI:

```python
app.include_router(relay.get_router(resolve_user_id=...))
```

The Teams Bot webhook then listens at `POST /api/messages`.

---

## 2. Stream modes: never use RAW

- **Teams webhook rate limits**: Teams Graph API and Bot Framework cap how often you may update one conversation. If you use `StreamMode.RAW` and edit the card on every token, you will get `HTTP 429 Too Many Requests` within about 3 seconds, and Microsoft may mute the bot for hours
- **What to use instead**:
  - **Default (`stream_mode="final"`)**: wait until the agent finishes, then send the body and aggregated Adaptive Card once
  - **Throttle (`stream_mode="throttle"`)**: if you must show a typing effect, use `ThrottledStreamBuffer` and batch edits on a window of at least 1.5 seconds

---

## 3. Reaction state machine

When a Teams user @mentions the bot:

1. **Immediate reaction ACK**: the gateway stamps `👀` on the user message in milliseconds and sends a `Typing` indicator to the channel
2. **Success**: after the agent replies, `settle()` switches the reaction to `✅`
3. **Failure**: on error or timeout, `try-finally` switches the emoji to `❌` and posts a clear message so the group is not left waiting forever
