# 01-foundations / Sessions and Topology

Enterprise IM (Teams / Lark) and Web clients do not share a conversation model. The design problem is keeping context clear in group chat, DMs, and threads — without mixing one person's turn into another.

---

## 1. Unified session descriptor: `ConversationKey`

Whatever the channel, the gateway normalizes the inbound event to a typed `ConversationKey` immediately:

```python
class ConversationKey(BaseModel):
    platform: str                # "web", "cli", "teams", "lark"
    chat_id: str                 # channel-internal chat / group ID
    thread_id: str | None = None # post ID / reply-root message ID
    reply_to_id: str | None = None
    sender_id: str | None = None # sender user ID
    tenant_id: str | None = None # tenant ID
    is_direct_message: bool = False
```

---

## 2. User-defined `SessionKeyResolver`

Most frameworks hard-code session isolation. Real products need different topologies:
- **Scenario A (support ticket)**: every speaker in the ticket group shares one context;
- **Scenario B (personal assistant)**: each person who @-mentions the bot in a group keeps a private context;
- **Scenario C (cross-tenant isolation)**: the same `chat_id` from two tenants must stay physically separate.

`agno-harness` therefore supports a **user-defined `SessionKeyResolver`**:

```python
from agno_harness import RelayApp
from agno_harness.sessions import ConversationKey

# Topology 1: shared group context
def shared_team_resolver(key: ConversationKey) -> str:
    if key.thread_id:
        # Everyone in the same thread shares context
        return f"{key.platform}:{key.chat_id}:{key.thread_id}"
    # Ordinary group chat aggregates by chat_id
    return f"{key.platform}:{key.chat_id}"

# Topology 2: tenant + ticket isolation
def ticket_tenant_resolver(key: ConversationKey) -> str:
    return f"tenant_{key.tenant_id}:ticket_{key.chat_id}:{key.sender_id}"

# Inject into RelayApp
relay = RelayApp(agent, session_resolver=shared_team_resolver)
```

---

## 3. Default topology

If you do not pass `session_resolver`, `SessionManager` uses this **default topology**:
- **1:1 DM (`is_direct_message=True`)**:
  the whole DM is one continuous session: `{platform}:{chat_id}`;
- **Group thread (`thread_id` present)**:
  isolate by sender inside the thread: `{platform}:{chat_id}:{thread_id}:{sender_id}`;
- **Ordinary group chat**:
  isolate by sender in the group: `{platform}:{chat_id}:{sender_id}`.

That prevents the leak where A asks about the weather, B asks “what did I just say?”, and the bot answers with A's private content.

---

## 4. 25-hour idle TTL

On IM, users do not click “new chat”. Unbounded history fills the token window and the bill.

### Rules

1. **Sliding refresh**: each user–agent turn updates `last_active_at`;
2. **25-hour idle close**:
   - If the user is silent for 25 hours, the next message archives the old session;
   - The system issues a new `agno_session_id` and the agent starts a fresh context;
   - 25 hours (not 24) avoids cutting a daily check-in that lands on the same clock time.
3. **Explicit reset**: `/reset`, `/new`, or `/clear` calls `close_session()` immediately and sends a system confirmation.

---

## 5. Persistence backends

- **`InMemorySessionStore`**: default in-memory dict; local tests and single-process debug;
- **`SQLiteSessionStore`**: `aiosqlite` embedded store; sessions survive process restart:
  ```python
  from agno_harness.sessions import SessionManager, SQLiteSessionStore

  store = SQLiteSessionStore(db_path="/data/sessions.db")
  session_manager = SessionManager(store=store)
  relay = RelayApp(agent, session_manager=session_manager)
  ```
