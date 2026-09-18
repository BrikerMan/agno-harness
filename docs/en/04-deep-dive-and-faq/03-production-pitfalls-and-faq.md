# Production pitfalls and FAQ

For architects and core developers. These are the pitfalls, FAQs, and PostgreSQL JSONB patterns that showed up in real production — including enterprise agents with hundreds of thousands of daily active users.

---

## 1. Five production rules

### 1. Never typewriter-edit IM messages per token (Rate-Limit Meltdown)

- **Pitfall**: in Teams or Lark, high-frequency `edit_message` to mimic a web typewriter gets you blocked in about 3 seconds (`HTTP 429 Too Many Requests`)
- **Fix**: IM channels use `stream_mode="final"` (the default). Push once after the model finishes. If you must fake typing, use `stream_mode="throttle"` with a window of **at least 1.5 seconds**

### 2. Auth failure must stop the request — never silently become a guest (Fail-Fast & Fail-Loud)

- **Pitfall**: when `resolve_user_id` returns `None` or throws, the gateway keeps going as a shared Guest and mixes employee data
- **Fix**: `agno-harness` fail-fast at boot (missing `resolve_user_id` raises `ConfigurationError`) and returns `HTTP 401 Unauthorized` on runtime auth failure

### 3. Session topology must be business-decoupled (User-Defined SessionKeyResolver)

- **Pitfall**: DMs and groups share one `chat_id`, so colleagues in the same group pollute each other's context
- **Fix**: the **default topology** isolates DMs by conversation and isolates groups and posts by sender. You can also pass a `session_resolver` and compose keys from ticket IDs, customer IDs, and so on

### 4. Long model thinking needs webhook idempotency (Webhook Retry Storms)

- **Pitfall**: a 5-second chain-of-thought looks like a timeout; Lark/Teams retry 3 times and the same question runs 3 times in parallel
- **Fix**: `RelayApp` sets `enable_deduplication=True` and silently drops duplicate `event_id`s inside a sliding window

### 5. Platform reactions must close in `try...finally` (Reaction Ghost State)

- **Pitfall**: you stamp a “thinking” reaction, the model crashes, and the emoji hangs forever
- **Fix**: `RelayApp` settles this for you: `✅` on success, `❌` on failure. No ghost thinking state

---

## 2. Dual Storage and PostgreSQL JSONB

Message audit in `agno-harness` follows **Dual Storage**:

```sql
CREATE TABLE IF NOT EXISTS agno_message_audits (
    id BIGSERIAL PRIMARY KEY,
    direction VARCHAR(16) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    chat_id VARCHAR(128) NOT NULL,
    thread_id VARCHAR(128),
    sender_id VARCHAR(128),
    session_id VARCHAR(128) NOT NULL,
    text TEXT NOT NULL,             -- cleaned Markdown after parse (semantic search and replay)
    raw_text TEXT,                  -- original rich text / HTML (high-fidelity restore)
    raw_payload_json JSONB,         -- full raw platform JSON (disputes and 100% audit)
    cards_json JSONB,               -- rendered interactive card JSON
    extra_json JSONB,               -- structured attachments, callback action IDs, return values
    created_at TIMESTAMPTZ NOT NULL
);
```

### PostgreSQL JSONB queries

Because the dialect-adaptive `JSONVariant` maps to binary `JSONB` on PostgreSQL:

```sql
-- 1. Messages that record a specific approval action
SELECT id, sender_id, text, extra_json->>'action_id' AS action
FROM agno_message_audits
WHERE extra_json->>'action_id' = 'agno.hitl.resume';

-- 2. Questions that uploaded a PDF attachment
SELECT chat_id, text, extra_json->'attachments'
FROM agno_message_audits
WHERE extra_json @> '{"attachments": [{"content_type": "application/pdf"}]}';

-- 3. High-performance GIN indexes in production
CREATE INDEX idx_audits_extra_gin ON agno_message_audits USING gin (extra_json);
CREATE INDEX idx_audits_raw_gin ON agno_message_audits USING gin (raw_payload_json);
```

---

## 3. FAQ

### Q1: Why avoid a global `@relay.action` decorator in favor of Class-First?

**A**:
`@relay.action("btn")` looks simple in a demo. In a large production project it breaks down:

1. **Import cycles**: the card file imports the `relay` instance to register a handler; the entry that builds `relay` imports the card. Cross-file cycles become `ImportError: cannot import name ...`
2. **Broken cohesion**: render, resolve, and click handlers live in different files. Change one field and you miss the others
3. **Namespace collisions**: two modules register the same action name (for example `"submit"`). The global dict silently overwrites

**Class-First** keeps schema, resolver, renderer, and action handler in one self-contained class. Those defects go away.

### Q2: Why does a group chat need a chime-in policy?

**A**:
If the bot can read group messages in Lark or Teams, every exchange between two people becomes an event.
Without a chime-in filter, the bot runs the model on every line, floods the group, and multiplies token cost by tens.
`agno-harness` ships `MentionOnlyPolicy` (least privilege): **run only on DMs, or in a group when the bot is `@` mentioned**. Everything else is zero LLM spend.

### Q3: How do I debug Lark and Teams locally without a public IP or a filed domain?

**A**:
- **Lark**: use **WebSocket long-connection mode** (`LarkChannel(use_websocket=True)`). Run locally and talk to Lark cloud in real time — no public IP, port map, or Nginx
- **Teams**: run `agno-harness teams onboard`, then expose localhost with free `devtunnel` or `ngrok` (for example `ngrok http 8000`)
