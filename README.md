# agno-relay

[![PyPI Version](https://img.shields.io/pypi/v/agno-relay.svg)](https://pypi.org/project/agno-relay/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> **Universal Multi-Channel Production Gateway for AGNO Agents**  
> Run your AGNO agent anywhere: **Web (AG-UI)**, **Microsoft Teams**, **Lark (Feishu)**, and **CLI** with zero changes to your agent logic.

[中文文档 (Chinese)](README_zh.md) | [Architecture Specification (SPEC.md)](SPEC.md) | [Agent Guidelines (AGENTS.md)](AGENTS.md)

---

## Why agno-relay?

Building an AI Agent in Python is fun. Deploying it into real-world enterprise environments (Teams, Lark, Web) is brutal:

- **Rate-Limit Meltdown**: Streaming tokens to Teams or Lark via `patch_message` triggers `HTTP 429 Too Many Requests` within seconds.
- **Card Notification Floods**: A search tool emitting 10 results sends 10 separate chat messages, blowing up team notifications.
- **Cross-Talk & State Confusion**: In group chats, multiple colleagues @mentioning the bot easily corrupt each other's agent memory.
- **Context Explosion**: Long-running enterprise conversations blow past LLM token limits and destroy KV cache hit rates.

`agno-relay` solves all of this out-of-the-box. It is not just a chat adapter; it is an **industrial-grade production gateway**.

---

## Key Features

- 🛡️ **Rate-Limit Immune**: Built-in `stream_mode="final"` (zero mid-stream edits, instant reaction ACK + typing heartbeat) and `stream_mode="throttle"` (adaptive 1.5s flush window).
- 🧩 **Class-First Card Components**: One card class defines the LLM prompt, the server resolver, the Teams Adaptive Card, and the Lark Interactive Card. No circular imports.
- 📦 **N-in-1 Card Aggregation**: Automatically bundles multiple stream items into a single, elegant IM card upon block completion.
- ⏳ **Ivy-Grade Session Topology**: Isolates conversation state per person per thread (`thread_key:sender`) with an intelligent **25-hour idle auto-close TTL** (spanning yesterday's afternoon into today's morning seamlessly).
- ⚡ **In-Context Checkpointing**: In-context memory compression preserving 100% KV Cache prefix hits.
- 🔒 **Clean Seal**: Prevents OpenAI 400 Bad Request errors by cleanly closing dangling tool calls on abort or timeout.
- 🌐 **Zero-Ingress Lark Long Connection**: Run Lark bots locally via WebSocket with zero public URLs or ngrok tunnels.

---

## Quickstart

### Installation

```bash
# Core (Web AG-UI & CLI)
pip install agno-relay

# With Teams support
pip install "agno-relay[teams]"

# With Lark support
pip install "agno-relay[lark]"

# Full installation
pip install "agno-relay[all]"
```

### 1-Minute Example

```python
from agno.agent import Agent
from agno_relay import RelayApp, CLIChannel, LarkChannel

# 1. Define your standard AGNO Agent
agent = Agent(name="Assistant", instructions="You are a helpful assistant.")

# 2. Mount it onto agno-relay
app = RelayApp(agent)
app.add_channel(CLIChannel())
app.add_channel(LarkChannel(app_id="...", app_secret="...", use_websocket=True))

if __name__ == "__main__":
    app.serve()
```

---

## License

MIT © [Eliyar Eziz](https://github.com/eliyar-eziz)
