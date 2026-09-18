# 06. Advanced and Fancy Modules

Besides single-chat and multi-channel sessions, `agno-harness` ships a set of “fancy” modules for higher-end enterprise cases.

This chapter covers **the user request sent to the model (with a timestamp)**, **attachment OCR**, **chime-in policy**, **colored observability logs**, and a **PostgreSQL native JSONB audit trail**.

Why the user request sent to the LLM includes time: [User Query Envelope](../02-interactions/05-user-query.md). It is on by default; you usually do not need to configure it.

---

## 1. Pluggable attachment processors (`AttachmentProcessor`)

In office chat, people often send PDF contracts, Excel reports, or prototype images.

`agno-harness` normalizes platform-specific file fetch and download into `InboundAttachment`, and exposes a pluggable `AttachmentProcessor` protocol:

```python
from agno_harness import AttachmentProcessor, ChannelEvent, InboundAttachment

class EnterpriseDocMindProcessor(AttachmentProcessor):
    """Custom attachment processor: wire your own OCR, cloud DocMind, or Textract."""

    async def process(
        self,
        attachments: list[InboundAttachment],
        event: ChannelEvent,
    ) -> str | None:
        extracted_texts = []
        for att in attachments:
            # Download by platform and att.id, then run OCR
            # You own the control plane: tenant checks, file allow-lists, etc.
            extracted_texts.append(f"[File: {att.name} content]:\nThis contract specifies a 30-working-day delivery window...")
        
        # Return extracted text; agno-harness injects it into the run context (RunAgentInput.context)
        return "\n\n".join(extracted_texts) if extracted_texts else None

# Mount on RelayApp
relay = RelayApp(
    runtime=runtime,
    attachment_processor=EnterpriseDocMindProcessor(),
)
```

---

## 2. Chime-in and least-privilege policy (`ChimeInPolicy`)

In a large group, a bot with message-read permission receives every line. Calling the model on each one floods the chat and multiplies token cost.

`agno-harness` ships a **chime-in policy engine**:

```python
from agno_harness import MentionOnlyPolicy, KeywordChimeInPolicy

# Policy 1: least privilege (DMs, or group messages that @ the bot)
policy = MentionOnlyPolicy(bot_names=["Assistant", "HelpBot"])

# Policy 2: keyword chime-in (for example "help", "outage", "bug")
keyword_policy = KeywordChimeInPolicy(keywords=["outage", "alert", "can someone look at this"])

relay = RelayApp(
    runtime=runtime,
    chime_in_policy=policy,  # messages that miss the policy are dropped with 0 LLM cost
)
```

---

## 3. Colored relay logging

Debugging is harder when logs have no channel or session context. `agno-harness` provides colored logs with channel badges and conversation tracing:

```python
from agno_harness import setup_relay_logging, get_relay_logger

# 1. Local development: readable logs with colored badges
setup_relay_logging(level="DEBUG", format_type="console")

# 2. Kubernetes / container production: one structured JSON line
# setup_relay_logging(level="INFO", format_type="json")

log = get_relay_logger("custom_service")
log.info("Processing inbound event", extra={"chat_id": "c-101", "platform": "lark"})
```

### Console output

```text
12:00:01 INFO  [LARK]  Received message event: 'review the contract' (chat_id=oc_123, sender_id=ou_456)
12:00:02 INFO  [AGNO]  Starting LLM turn with gpt-4o (thread_id=lark:oc_123:ou_456)
12:00:04 INFO  [TEAMS] Dispatched Adaptive Card to user (action_id=order.approve)
```

---

## 4. Dual storage and PostgreSQL JSONB

For money approvals, production changes, or customer disputes, you need a **complete raw-payload evidence trail**.

`agno-harness` uses **dual storage**:
- `text`: cleaned Markdown (replay and vector search);
- `raw_text`: original HTML or uncleaned text (high-fidelity restore);
- `raw_payload_json`: the platform's full JSON webhook (incident review);
- `extra_json`: attachment metadata, action IDs, and parameters.

On PostgreSQL the store uses native binary **`JSONB`**:

```sql
-- History that includes a specific procurement approval action
SELECT id, sender_id, text, extra_json->>'action_id' AS action
FROM agno_message_audits
WHERE extra_json->>'action_id' = 'approve_deploy';

-- Employee consults that uploaded a PDF
SELECT chat_id, text, extra_json->'attachments'
FROM agno_message_audits
WHERE extra_json @> '{"attachments": [{"content_type": "application/pdf"}]}';

-- Production GIN indexes
CREATE INDEX idx_audits_extra_gin ON agno_message_audits USING gin (extra_json);
CREATE INDEX idx_audits_raw_gin ON agno_message_audits USING gin (raw_payload_json);
```
