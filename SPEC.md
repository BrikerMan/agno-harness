# SPEC: agno-relay Protocol & Architecture

> **Version**: 1.0.0-draft  
> **Status**: Living Standard  
> **Audience**: Architects, Developers, and Coding Agents

---

## 1. Vision & Core Philosophy

**"Write once, run anywhere."**

`agno-relay` is the universal enterprise-grade production gateway for AGNO agents. It bridges AGNO's reasoning engine across four primary target channels:
1. **Web**: Full-fidelity AG-UI protocol over Server-Sent Events (SSE) with 0s keepalive pings and disconnection recovery (`/attach`).
2. **Teams**: Microsoft 365 Agents SDK over FastAPI, with rate-limited adaptive flushing, reaction-ACK, and native Adaptive Cards.
3. **Lark (Feishu)**: WebSocket long-connection mode (zero public ingress required), interactive card v2 callback handling, and typing fallbacks.
4. **CLI**: Rich terminal UI for rapid local testing, session replay, and deterministic debugging.

### Two Cardinal Rules

1. **"The model chooses; the server supplies the facts."**
   The LLM should never be prompted to emit huge, hallucinated JSON blocks (like complete Adaptive Cards or styling markup). The model emits compact semantic identifiers (`id`, `note`) inside an XML fence (`<stream-ui>`). The server asynchronously resolves authoritative facts from databases/APIs and binds them into platform-specific visual structures.

2. **"No Streaming Meltdown."**
   Token-by-token streaming is designed for Web SSE. Calling message patch APIs on IM platforms (Teams/Lark) at token frequency triggers `HTTP 429 Too Many Requests`. Channels default to `stream_mode="final"` (single clean turnaround with typing indicators) or `stream_mode="throttle"` (adaptive rate-limited buffering).

---

## 2. Channel Protocol

Every channel adapter implements the asynchronous `Channel` protocol:

```python
from collections.abc import AsyncIterator
from typing import Any, Protocol
from pydantic import BaseModel

class Channel(Protocol):
    name: str

    async def listen(self) -> AsyncIterator["ChannelEvent"]:
        """Listen for incoming messages, commands, or card action callbacks."""
        ...

    async def send(self, destination: "ConversationKey", message: "OutboundMessage") -> str:
        """Send a completed message (text and/or card). Returns platform message_id."""
        ...

    async def stream_chunk(self, destination: "ConversationKey", message_id: str, delta: str) -> None:
        """Throttled progressive update of an in-flight message."""
        ...

    async def ack(self, event: "ChannelEvent", emoji: str = "👀") -> Any:
        """Acknowledge receipt immediately (e.g. Teams reaction, Lark reaction)."""
        ...

    async def settle(self, destination: "ConversationKey", ack_token: Any, emoji: str = "✅") -> None:
        """Mark completion on the platform acknowledgment."""
        ...

    async def typing(self, destination: "ConversationKey", active: bool) -> None:
        """Start or stop typing indicator heartbeats."""
        ...
```

---

## 3. Session Topology & Lifecycle

### 3.1 ConversationKey

IM platforms have hierarchical conversation structures. `agno-relay` captures this via `ConversationKey`:

```python
class ConversationKey(BaseModel):
    platform: str                    # "web" | "cli" | "teams" | "lark"
    chat_id: str                     # channel ID or conversation ID
    thread_id: str | None = None     # reply thread root or meeting topic
    reply_to_id: str | None = None   # immediate parent message ID
    tenant_id: str | None = None     # tenant or organization ID
    sender_id: str | None = None     # user unique identity
```

### 3.2 Ivy-Style Session Boundary

To avoid cross-talk in team chats:
- **1-on-1 Direct Message**: `session_key = conversation_id` (entire DM shares one continuous context).
- **Channel / Team Topic**: `session_key = f"{thread_key}:{sender_id}"` (isolated per participant per thread).
- **Group Chat**: `session_key = f"{conversation_id}:{sender_id}"`.

### 3.3 25-Hour Idle Auto-Close TTL

Active sessions maintain a `last_active_at` timestamp.
- If `now() - last_active_at > 25 hours`: the active session is retired (`finished_at = now()`), and a fresh `agno_session_id` is assigned.
- This spans the natural "yesterday afternoon to today morning" work boundary without unbounded context bloat.
- Commands like `/reset` or `/new` force immediate session retirement.

---

## 4. Class-First Card Component Standard

Cards must be **self-contained classes** in their own modules. They MUST NOT depend on instance decorators like `@catalog.renderer(...)` which cause circular import deadlocks.

```python
class MyItem(ItemSchema):
    schema_name = "my-item"
    id: int
    note: str | None = None

    async def resolve(self, ctx: Any = None) -> dict[str, Any]:
        """Server-side fact lookup."""
        return {"title": "Real Title", "score": 9.8}

    def render_teams(self, resolved: dict[str, Any]) -> dict[str, Any]:
        """Fragment inside Teams Adaptive Card."""
        return {"type": "TextBlock", "text": f"★ {resolved.get('title')}"}

    def render_lark(self, resolved: dict[str, Any]) -> dict[str, Any]:
        """Fragment inside Lark Interactive Card v2."""
        return {"tag": "div", "text": {"tag": "lark_md", "content": f"**{resolved.get('title')}**"}}
```

### Batch Item Aggregation
When a block emits multiple items (`body="items"`):
1. In Web: each line emits an asynchronous `ui.item` frame over SSE.
2. In Teams / Lark: individual items are buffered until `ui.block.end`. All items are aggregated into **ONE unified Adaptive Card or Lark Interactive Card**, preventing chat flood.
