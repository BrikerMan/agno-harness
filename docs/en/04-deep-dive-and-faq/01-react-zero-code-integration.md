# Zero-change React integration and DevTools

The product shell (iron rules, sidebar, stick-to-bottom, attach) is the [Web / React five steps](../03-clients/01-web-react/README.md). This page is only: **an existing AG-UI client can point at this service with zero breaking changes**, plus the DevTools panel contract.

Many product teams already have a chat UI on standard AG-UI / Server-Sent Events (SSE).

When you upgrade to `agno-harness`, **the frontend needs no breaking rewrite**. You stay 100% compatible and immediately get stronger observability and debugging.

---

## 1. Why the frontend can stay at zero code change

The execution kernel `AgentRuntime` follows the standard AG-UI transport:

- **Same endpoint**: `POST /agui/runs` (or a custom prefix)
- **Same request**: standard `RunAgentInput` (`thread_id`, `run_id`, `messages`, `state`, `context`)
- **Same SSE frames**: `event: run_started`, `event: text_message_content`, `event: custom`, `event: run_finished`
- **Same keepalive**: `: ping\n\n` comment frames every 5 seconds by default, so ALB / Nginx never idle-cut the stream

Point an existing AG-UI React client at the `agno-harness` port and chat plus cards come back.

---

## 2. Opt-in enhancements

Zero change is enough to run. A few extra lines light up the advanced features:

### A. Human-in-the-loop (HITL)

When a backend tool needs confirmation, the frontend gets a `CUSTOM` frame:

```json
{
  "event": "custom",
  "name": "run.paused",
  "value": {
    "action_id": "agno.hitl.resume",
    "tool_call_id": "call_abc123",
    "tool_name": "restart_production_service",
    "pause_type": "confirmation",
    "tool_args": {"service": "order-api"}
  }
}
```

**Frontend response**:
On `name === "run.paused"`, show a dialog or approval card. After the user clicks Approve, POST to `/agui/runs` again with a resume payload:

```typescript
const resumePayload = {
  thread_id: currentThreadId,
  run_id: uuidv4(),
  tools: [],
  messages: [
    {
      id: uuidv4(),
      role: "tool",
      tool_call_id: "call_abc123",
      content: JSON.stringify({ accepted: true }) // send the human approval
    }
  ]
};
```

### B. Action broadcast (`action.executed` / `action.resolved`)

When someone clicks an approval button in a Lark group or Teams, `agno-harness` broadcasts on the web Custom stream:

```json
{
  "event": "custom",
  "name": "action.resolved",
  "value": {
    "tool_call_id": "call_abc123",
    "decision": "approved",
    "resolver": "Alice (via Lark)"
  }
}
```

The web client listens, closes the pending approval modal, and shows “Alice approved this on Lark” — live state across channels.

---

## 3. Built-in React Debug DevTools

To debug long threads, multi-turn context, token spend, and stream drops, host a Debug panel that consumes `frames` / `violations` (contract: [Web 05](../03-clients/01-web-react/05-hitl-cards-devtools.md)):

- **Components**:
  - `DebugPanel.tsx`: embedded debug drawer
  - `TimelineTab.tsx`: millisecond timeline from request → time to first token (TTFT) → tool calls
  - `EventsTab.tsx`: raw SSE frames and a JSON tree
  - `ProtocolTab.tsx`: protocol checks (dangling tool calls, out-of-order frames)
  - `ChunksTab.tsx`: raw chunk replay

### How to mount it

```tsx
import { DebugPanel } from './agui-devtools/DebugPanel';

function App() {
  return (
    <div className="relative">
      <ChatInterface />
      {/* show only in development or for admins */}
      {process.env.NODE_ENV === 'development' && (
        <DebugPanel />
      )}
    </div>
  );
}
```
