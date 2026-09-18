# 02. IM close-loop and Class-First actions

There is no React form on IM. `MessageCollector` turns `run.paused` into a native card, carrying `session_id` and request `meta`.

- Teams: Adaptive Card with `Action.Submit`
- Lark: Interactive Card v2 primary / danger buttons

```json
{
  "action_id": "agno.hitl.resume",
  "tool_call_id": "call_abc123",
  "pause_type": "confirmation",
  "accepted": true,
  "session_id": "session_teams_user1",
  "meta": { "platform": "teams", "operator": "alice" }
}
```

With `SQLAlchemyActionStore`, the row is `status="pending"`.

On allow / deny:

1. The webhook ACKs with a reaction immediately;
2. The action store becomes `approved` / `rejected`;
3. `CustomEventStore` writes `action.resolved` (`toolCallId`, `decision`, `userId`, `platform`, `meta`). Web replay of the same thread sees the cross-channel decision;
4. A trailing `ToolMessage` wakes `runtime.stream_events`;
5. `MessageCollector` sends the result once and stamps the original card.

## Do not pile `@relay.action` in production

The card file imports `relay` from `app.main`; the entry file imports the card so the decorator registers — two files and you have a cycle. The action also leaves the class.

**Prefer:** `handle_action` on the card class.  
**Composition root:** pass `action_handlers={...}` at the entry.

```python
class FeedbackCard(BlockSchema):
    schema_name: ClassVar[str] = "feedback-card"

    @classmethod
    async def handle_action(cls, action: str, payload: dict, event: ChannelEvent):
        if action == "thumbs_up":
            await db.record_like(event.key.chat_id, user_id=event.key.sender_id)
            return None  # silent stamp, no junk bubble
        if action == "view_details":
            return f"Order status: {(await db.get_order(payload.get('order_id'))).status}"
        return None
```

Same verb `submit` is namespaced by `schema_name`: `feedback:submit`, or a payload `{"schema": "feedback", "action": "submit"}`.

```python
relay = RelayApp(
    runtime,
    card_catalog=catalog,
    action_handlers={"thumbs_up": handle_thumbs_up, "heart": handle_heart},
)
```

`None` → reaction settle. `OutboundMessage(text="", extra={"settle_emoji": "❤️"})` picks the emoji. Afterwards `action.executed` is stored so Web replay can see it.

No LLM tokens. See [01 cards](../01-class-first-cards.md).

Next: [03 Web resume](03-web-resume.md).
