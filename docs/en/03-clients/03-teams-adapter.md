# 03-clients / Microsoft Teams adapter

`TeamsChannel()` reads `AGNO_HARNESS_TEAMS_*` and speaks Bot Framework. The copy-paste server is the [Teams cookbook](../00-agent-cookbook/03-teams-bot-agent.md). This page is the contract behind that server.

---

## 1. Quick start

```python
from fastapi import FastAPI
from agno.agent import Agent
from agno_harness import AgentRuntime, RelayApp, TeamsChannel

runtime = AgentRuntime(agent=Agent(name="Teams Assistant", instructions="You are an office assistant."))
relay = RelayApp(runtime=runtime)
relay.add_channel(TeamsChannel())  # or TeamsChannel(bot_app_id="...", bot_app_password="...", tenant_id="...")

app = FastAPI(lifespan=relay.lifespan)
app.include_router(relay.get_router(allow_anonymous=True))
```

`RelayApp` takes an `AgentRuntime`, not a raw agent. The tenant parameter is `tenant_id` (`bot_tenant_id` is accepted as an alias).

The webhook is `POST /api/v1/channels/teams/messages`. A router prefix is prepended, so `prefix="/agent"` makes the Azure Messaging endpoint `POST /agent/api/v1/channels/teams/messages`.

`agno-harness teams doctor` prints that endpoint, checks the env, and requests a connector token.

---

## 2. What zero-arg `TeamsChannel()` does

- **JWT.** `Authorization: Bearer` is checked against the Bot Framework JWKS. Audience is the App ID. No token or a bad token is HTTP 401. Missing App ID or password is HTTP 503, not a fake 200.
- **Reply.** The inbound `serviceUrl` and conversation id are cached. `send()` posts a message activity with the client-credentials token. It returns the connector activity id. It does not invent `teams_msg_...`.
- **serviceUrl.** Only `https` hosts under `*.botframework.com`, `*.botframework.azure.us`, and `*.smba.trafficmanager.net`. Anything else raises before any outbound request.
- **Typing.** `ack()` and `typing(active=True)` post a `typing` activity. Teams connector reactions are not arbitrary emoji, so `settle()` does not stamp 👀/✅/❌ on the user message. The reply itself is the visible result.
- **Activities.** `message` and `invoke` (Adaptive Card actions) become `ChannelEvent`s. `conversationUpdate` and the rest are logged and ignored. Card invokes answer with an invoke response so Teams does not show a failure.
- **Mention-only.** Group and channel traffic uses `MentionOnlyPolicy` unless you pass `chime_in_policy`. Direct messages still reply. CLI is unchanged. An explicit `RelayApp(chime_in_policy=...)` overrides the channel.
- **Threads.** A channel topic reply uses the `messageid` in `conversation.id` as the session thread. A channel root post uses `teamsChannelId`. `teamsTeamId` is not the thread, so two channels in one team do not share context.

---

## 3. Tenant and token authority

| `AGNO_HARNESS_TEAMS_TENANT_ID` | Token URL |
|---|---|
| set | `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` |
| empty | `https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token` |

Single-tenant bots must set the tenant. Multi-tenant bots leave it empty. Scope is `https://api.botframework.com/.default`.

---

## 4. Stream modes

Do not use `StreamMode.RAW` on Teams. The default `final` mode sends one message when the agent finishes. `throttle` batches edits on a window of at least 1.5 seconds.

---

## 5. Attachments

Inbound files are metadata (`contentUrl`, name, content type) on `InboundAttachment`. The channel does not download them. Pass an `AttachmentProcessor` to `RelayApp` when you want bytes or extracted text. Download URLs yourself only after you trust the host.

---

## 6. Install

```bash
uv add "agno-harness[teams,fastapi]"
```

`teams` installs PyCryptodome for RS256 checks. The channel posts through `httpx`; it does not depend on the Microsoft Agents SDK.
