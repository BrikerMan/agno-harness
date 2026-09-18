# AG-UI wire protocol spec

This document defines the wire frames and lifecycle that the `agno-harness` execution engine emits to the transport layer (web SSE, WebSocket, debug tap).

If you are writing a custom frontend, a proxy, or an AI coding agent that reverse-engineers the protocol, this spec is the source of truth.

---

## 1. Run lifecycle

A standard agent turn must follow this one-way state machine:

```
[RUN_STARTED]
      │
      ├─► [REASONING_MESSAGE_START] ──► [REASONING_MESSAGE_CONTENT]* ──► [REASONING_MESSAGE_END]
      │
      ├─► [TOOL_CALL_STARTED] ──► [TOOL_CALL_ARGS]* ──► [TOOL_CALL_COMPLETED]
      │
      ├─► [TEXT_MESSAGE_START] ──► [TEXT_MESSAGE_CONTENT]* ──► [TEXT_MESSAGE_END]
      │
      ├─► [CUSTOM (ui.block.start | ui.item | ui.block.end)]*
      │
      ├─► [CUSTOM (run.paused)] (optional: pause and wait when HITL fires)
      │
      ▼
[RUN_FINISHED] (or an AgentRunFailed error frame)
```

---

## 2. Core wire frames

Each event on the SSE stream looks like:

```http
event: <event_name>
data: <json_string>

```

### 2.1 RUN_STARTED

```json
{
  "event": "run_started",
  "thread_id": "teams:chat_123:user_456",
  "run_id": "run-a1b2c3d4",
  "metadata": {
    "platform": "web",
    "agent_name": "DevOps Assistant"
  }
}
```

### 2.2 TEXT_MESSAGE_CONTENT (typewriter delta)

```json
{
  "event": "text_message_content",
  "message_id": "msg-987",
  "delta": "Hi — looking that up now"
}
```

### 2.3 REASONING_MESSAGE_CONTENT (thinking-chain delta)

For reasoning models such as DeepSeek-R1 and o1, thinking is physically isolated from the final answer:

```json
{
  "event": "reasoning_message_content",
  "message_id": "msg-987",
  "delta": "The user wants yesterday's production error logs. Call the log search tool first..."
}
```

### 2.4 TOOL_CALL_STARTED & TOOL_CALL_COMPLETED

```json
// start the call
{
  "event": "tool_call_started",
  "tool_call_id": "call_12345",
  "tool_name": "query_logs",
  "args": {"service": "payment", "level": "ERROR"}
}

// complete and return the result
{
  "event": "tool_call_completed",
  "tool_call_id": "call_12345",
  "tool_name": "query_logs",
  "result": {"count": 3, "logs": ["Connection timeout to DB"]}
}
```

### 2.5 CUSTOM: run.paused (HITL hold)

```json
{
  "event": "custom",
  "name": "run.paused",
  "value": {
    "action_id": "agno.hitl.resume",
    "tool_call_id": "call_999",
    "tool_name": "drop_temp_tables",
    "pause_type": "confirmation",
    "tool_args": {"database": "prod_analytics"},
    "session_id": "teams:chat_1:user_1"
  }
}
```

### 2.6 CUSTOM: ui.block / ui.item (adaptive cards)

```json
// start a card container
{
  "event": "custom",
  "name": "ui.block.start",
  "value": {"name": "movie-list", "props": {"title": "Recommended movies"}}
}

// insert a resolved card item
{
  "event": "custom",
  "name": "ui.item",
  "value": {
    "name": "movie",
    "data": {"id": 101},
    "resolved": {"title": "Interstellar", "rating": 9.4, "year": 2014}
  }
}

// end the card container
{
  "event": "custom",
  "name": "ui.block.end",
  "value": {}
}
```

### 2.7 RUN_FINISHED

```json
{
  "event": "run_finished",
  "thread_id": "teams:chat_123:user_456",
  "run_id": "run-a1b2c3d4",
  "status": "completed"
}
```

---

## 3. Error and cancel frames

If an uncaught exception fires during the run, or the user cancels:

1. **Cancel**: emit `EVENT_RUN_CANCELLED`, then close dangling tool calls (`close_dangling_tool_calls`) so persistence is not left half-written
2. **Exception**: emit a wrapped `AgentRunFailed` frame, and call `channel.settle(emoji="❌")` so the chat window shows the failure
