# 01. HITL protocol

The stream ends normally, then you wait. Not an interrupt. There is no separate HITL event type.

| Axis | Agno native HITL | Card business action |
| --- | --- | --- |
| Trigger | Agent pauses on a gated tool | User clicks a business button |
| State machine | `RunPaused`, stream finished; `resume_paused_run` wakes it | Webhook, unrelated to the agent |
| Scenes | Delete confirm, missing fields, multi-select, browser geolocation | Like, watch later, outbound link |
| Protocol | `CUSTOM run.paused` → trailing `role: "tool"` | `action_id` + payload |

## Backend declaration

```python
from agno.tools import tool

@tool(requires_confirmation=True)
async def delete_file(path: str) -> str: ...

@tool(requires_user_input=True, user_input_schema=[
    {"name": "recipient", "description": "Recipient email", "fieldType": "string", "required": True},
])
async def send_email(recipient: str, subject: str, body: str) -> str: ...

@tool(user_feedback_schema=[{
    "name": "Pick a deploy target",
    "fieldType": "choice",
    "options": ["staging", "production"],
    "multiSelect": False,
}])
async def deploy_service(environment: str) -> str: ...
```

`external_execution` is declared on `RunAgentInput.tools` and runs in the browser. `user_feedback` uses Agno `UserFeedbackTools`; the model calls `ask_user`.

## Pause frames

`translator.complete()` yields `run.paused` first, then `TOOL_CALL_*`, then `RUN_FINISHED`. `isStreaming === false`. If that id is already in `pendingTools` when START arrives, the card is `waiting` — do not paint `running` first.

```json
{
  "pauseType": "confirmation",
  "toolCallId": "…",
  "toolName": "delete_file",
  "toolArgs": { "path": "/tmp/old-report.csv" },
  "userInputSchema": null
}
```

Only `user_input` / `user_feedback` carry `userInputSchema`. Build the form from that. For `user_input`, `name` is the field. For `user_feedback`, `name` is the question text (the `selections` key on resume), plus `options` and `multiSelect`.

## One resume shape

`POST /api/v1/channels/web/agui` again with a trailing `role: tool` (`detect_resume` only looks at the tail). Do not open a new user bubble. The answer is Agno AG-UI resume, not `requirement.confirm()` in Python.

| `pauseType` | Answer `content` |
| --- | --- |
| `confirmation` | `{"accepted": true}` / `{"accepted": false, "note": "…"}`. Deny does not run the tool |
| `user_input` | `{"values": {"fieldName": …}}` |
| `user_feedback` | `{"selections": {"question text": ["label", …]}}`. Key is the question; value must be a label array |
| `external_execution` | Raw result, or `{"error": "…"}` |

**Anti-patterns:**

1. Treat HITL as an interrupt;
2. Send the answer as a new `user` message;
3. Wrap confirmation as `{"values": {"accepted": false}}` — that `false` is a parameter and the tool still runs.

Next: [02 IM and class actions](02-im-and-class-actions.md).
