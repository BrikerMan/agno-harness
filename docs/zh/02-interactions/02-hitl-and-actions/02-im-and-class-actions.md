# 02. IM 闭环与 Class-First 动作

IM 里没有 React 表单。`MessageCollector` 吃到 `run.paused` 后编原生卡，带上 `session_id` 与请求 `meta`。

- Teams：`Action.Submit` Adaptive Card
- 飞书：Interactive Card v2 主色 / 警示按钮

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

配了 `SQLAlchemyActionStore` 时写入 `status="pending"`。

用户点允许 / 拒绝：

1. Webhook 立刻 Reaction ACK；
2. ActionStore 更新 `approved` / `rejected`；
3. `CustomEventStore` 落 `action.resolved`（`toolCallId`、`decision`、`userId`、`platform`、`meta`）。Web 回放同一 Thread 能看到跨渠道决议；
4. 构造 trailing `ToolMessage`，`runtime.stream_events` 唤醒；
5. `MessageCollector` 一次发回结果，原卡盖戳。

## 不要用全局 `@relay.action` 堆生产

卡片文件要 `from app.main import relay`，入口又要 import 卡片才能注册 —— 超过两个文件就循环导入。动作逻辑也被抽离出类。

**推荐：** 动作写在卡片类 `handle_action`。  
**组合根：** 入口显式 `action_handlers={...}`。

```python
class FeedbackCard(BlockSchema):
    schema_name: ClassVar[str] = "feedback-card"

    @classmethod
    async def handle_action(cls, action: str, payload: dict, event: ChannelEvent):
        if action == "thumbs_up":
            await db.record_like(event.key.chat_id, user_id=event.key.sender_id)
            return None  # 静默盖戳，不发垃圾气泡
        if action == "view_details":
            return f"订单状态：{(await db.get_order(payload.get('order_id'))).status}"
        return None
```

同名 `submit` 靠 `schema_name` 做命名空间：`feedback:submit` 或 payload 带 `{"schema": "feedback", "action": "submit"}`。

```python
relay = RelayApp(
    runtime,
    card_catalog=catalog,
    action_handlers={"thumbs_up": handle_thumbs_up, "heart": handle_heart},
)
```

返回 `None` → Reaction settle。`OutboundMessage(text="", extra={"settle_emoji": "❤️"})` 自定义盖戳。执行后落 `action.executed`，Web 回放看得到。

不消耗 LLM Token。详见 [01 卡片](../01-class-first-cards.md)。

下一步：[03 Web 续上](03-web-resume.md)。
