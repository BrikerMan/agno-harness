# 03. Microsoft Teams Bot Agent

A minimal Teams bot: **Agent + DuckDuckGo only**. No extra cards or callbacks. Wizard → live chat.

---

## 1. Onboarding wizard

```bash
uv run agno-harness teams onboard
```

The wizard walks you through:

1. **Azure Portal** — create an Azure Bot.
2. **Least privilege** — `ChannelMessage.Read.Group`, `ChatMessage.Read`.
3. **Credentials** — App ID and Client Secret land in `.env`:

   ```env
   AGNO_HARNESS_TEAMS_APP_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   AGNO_HARNESS_TEAMS_APP_PASSWORD=your_azure_client_secret
   ```

It also writes a `manifest.json` app package template.

---

## 2. Architecture and sequence

```mermaid
sequenceDiagram
    autonumber
    actor User as Teams user
    participant Azure as Microsoft Teams / Azure Bot
    participant Channel as TeamsChannel
    participant Relay as RelayApp (idempotency + sessions)
    participant Runtime as AgentRuntime
    participant LLM as Agno Agent (LLM + Search)

    User->>Azure: @bot search Python 3.13 features
    Azure->>Channel: POST /agent/teams/messages

    rect rgb(240, 248, 255)
    Note over Azure,Channel: Fast ACK — stop the retry storm
    Channel->>Channel: Verify Bot Framework signature
    Channel-->>Azure: HTTP 200 OK immediately
    Channel->>Azure: React 🤔 or send typing
    Azure-->>User: Bot is thinking
    end

    rect rgb(245, 255, 250)
    Note over Channel,LLM: Async run
    Channel->>Channel: Strip HTML and <at> mentions
    Channel->>Relay: ChannelEvent (clean text + raw json)
    Relay->>Relay: DeduplicationCache
    Relay->>Runtime: stream_events(RunAgentInput)
    Runtime->>LLM: DuckDuckGo search
    LLM-->>Runtime: Answer
    Runtime-->>Relay: RunCompleted
    end

    rect rgb(255, 250, 245)
    Note over Channel,User: Deliver + settle reaction
    Relay-->>Channel: Aggregate (stream_mode="final")
    Channel->>Azure: Bot Connector: final Markdown
    Channel->>Azure: 🤔 → ✅ (or ❌ on error)
    Azure-->>User: One clean reply
    end
```

What the base already handles:

1. **Signature check** — Bot Framework JWT.
2. **HTML cleanup** — `<p>`, `<div>`, `<br>` become Markdown.
3. **Mention strip** — `<at>BotName</at>` never reaches the model as a prompt token.
4. **Rate-limit safety** — default `stream_mode="final"`, never edit per token.

---

## 3. Minimal server

Create `teams_agent_server.py`:

```python
"""teams_agent_server.py — minimal Teams bot"""
from fastapi import FastAPI
import uvicorn

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools.duckduckgo import DuckDuckGoTools
from agno_harness import (
    AgentRuntime,
    RelayApp,
    RelayConfig,
    TeamsChannel,
    setup_relay_logging,
)

setup_relay_logging(level="INFO")

agent = Agent(
    name="Teams Assistant",
    model=OpenAIChat(
        id=RelayConfig.llm_model(default="gpt-4o"),
        base_url=RelayConfig.llm_base_url() or None,
        api_key=RelayConfig.llm_api_key() or None,
    ),
    instructions=[
        "You are an office assistant in Microsoft Teams.",
        "Use DuckDuckGo for current facts.",
        "Reply in concise Markdown.",
    ],
    tools=[DuckDuckGoTools()],
    markdown=True,
)

runtime = AgentRuntime(agent=agent)

# enable_deduplication=True: drop Teams retries while search is still running
relay = RelayApp(runtime=runtime, enable_deduplication=True)

# Zero-arg init reads AGNO_HARNESS_TEAMS_APP_ID / AGNO_HARNESS_TEAMS_APP_PASSWORD
relay.add_channel(TeamsChannel())

app = FastAPI(title="Teams Agent Service", lifespan=relay.lifespan)

app.include_router(
    relay.get_router(
        prefix="/agent",
        allow_anonymous=True,  # Bot Framework signature is the auth
    )
)

if __name__ == "__main__":
    uvicorn.run("teams_agent_server:app", host="0.0.0.0", port=8000)
```

---

## 4. Tunnel and end-to-end

### A. Expose localhost

```bash
ngrok http 8000
# e.g. https://abc1234.ngrok-free.app
```

### B. Azure Messaging endpoint

Azure Portal → your Azure Bot → **Configuration** → Messaging endpoint:

```
https://abc1234.ngrok-free.app/agent/teams/messages
```

### C. Talk to the bot

1. Open Teams, DM the bot or `@` it in a group.
2. Ask: “Search the latest Python 3.13 features.”
3. Console should show a `[TEAMS]` badge, cleaned HTML, and `duckduckgo_search`.
4. The chat gets one Markdown reply — no 429, no broken formatting.
