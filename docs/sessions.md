# 会话拓扑与空闲生命周期 (Sessions)

`agno-relay` 沉淀了企业级 Bot（如 Ivy 等高频生产机器人）的会话治理经验，解决了多人在群聊中抢问时串台、长对话爆 Token 与 KV Cache 击穿的难题。

---

## 1. 统一会话拓扑模型 (`ConversationKey`)

在多渠道企业环境中，会话不是简单的单个字符串。`ConversationKey` 定义了统一拓扑：

```python
class ConversationKey(BaseModel):
    platform: str                    # "web" | "cli" | "teams" | "lark"
    chat_id: str                     # 单聊 ID、群聊 ID 或频道 ID
    thread_id: str | None = None     # 话题树根 ID (Teams 帖子 / 飞书话题)
    reply_to_id: str | None = None   # 上游触发的消息 ID
    sender_id: str | None = None     # 发送者唯一 ID (AAD Object ID / Lark Open ID)
    is_direct_message: bool = False  # 是否为 1 对 1 私聊
```

### 1.1 会话边界规则（Ivy-Style Boundary）

调用 `key.session_key` 自动计算边界：
1. **1-on-1 私聊**：`f"{platform}:{chat_id}"`（整场私聊共享连续记忆）；
2. **频道/帖子回复线程**：`f"{platform}:{chat_id}:{thread_id}:{sender_id}"`（同一帖子里不同同事各自隔离上下文）；
3. **普通大群聊**：`f"{platform}:{chat_id}:{sender_id}"`（同群多人 @Bot 互不干扰）。

---

## 2. 25 小时空闲超时自动闭环 (25h Idle TTL)

### 为什么是 25 小时？
- 如果设置 24 小时：昨天下午 3:00 聊过，今天下午 3:01 提问就会判定超时截断，体验突兀；
- 设置为 25 小时：**恰好平滑覆盖跨天的工作节奏**（昨天下午到今天上午依然保持上下文），同时杜绝数周前废弃会话带来的冗余负担。

### 自动闭环机制
- 每次交互自动更新 `last_active_at`；
- 当检测到当前时间距 `last_active_at` 超过 25 小时：
  1. 当前会话标记为结束（`finished_at = now()`）；
  2. 自动分配全新的 `agno_session_id`，开启全新干净的 Agent 会话；
  3. 用户无需手动点击任何清空按钮。

---

## 3. 指令级重置

用户发送 `/reset`、`/new`、`/clear` 时，`RelayApp` 会在进入大模型前直接拦截：
- 立即强制归档当前会话；
- 重置上下文并向用户返回系统确认通知；
- 0 Token 消耗。
