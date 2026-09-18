# 01-foundations / 会话拓扑与生命周期 (Sessions & Topology)

企业 IM（Teams / 飞书）与 Web 客户端的交互模式差异极大。如何让智能体在群聊、单聊、跨主题帖子（Thread）中保持清晰的上下文，同时避免“多人多轮对话串台”，是会话设计的核心问题。

---

## 1. 统一会话描述符：`ConversationKey`

无论请求来自哪个渠道，网关在接收到消息的第一时刻，都会将底层网络事件归一化为类型化的 `ConversationKey`：

```python
class ConversationKey(BaseModel):
    platform: str                # "web", "cli", "teams", "lark"
    chat_id: str                 # 渠道内部的会话 ID / 群 ID
    thread_id: str | None = None # 帖子 ID / 回复根消息 ID
    reply_to_id: str | None = None
    sender_id: str | None = None # 发送者用户 ID
    tenant_id: str | None = None # 租户 ID (企业标识)
    is_direct_message: bool = False
```

---

## 2. 用户自定义拓扑解析器 (User-Defined SessionKeyResolver)

在传统框架中，会话隔离规则往往被写死在底层框架代码里。但在实际企业场景中，不同的业务有截然不同的拓扑需求：
- **场景 A（客服工单）**：同一个工单群里，所有同事发言都属于同一个 Context；
- **场景 B（个人助手）**：群里 @ 机器人的每个人，机器人只和该用户独立维持上下文，互不干扰；
- **场景 C（跨租户隔离）**：即使 chat_id 相同，不同租户也必须物理隔离。

为了避免混乱，`agno-harness` 明确支持**用户自定义 `SessionKeyResolver`**：

```python
from agno_harness import RelayApp
from agno_harness.sessions import ConversationKey

# 自定义拓扑策略 1：群聊共享上下文 (Shared Channel Context)
def shared_team_resolver(key: ConversationKey) -> str:
    if key.thread_id:
        # 只要在同一个 Thread/帖子下，所有人共享上下文
        return f"{key.platform}:{key.chat_id}:{key.thread_id}"
    # 普通群聊按 chat_id 聚合
    return f"{key.platform}:{key.chat_id}"

# 自定义拓扑策略 2：租户 + 工单多维隔离
def ticket_tenant_resolver(key: ConversationKey) -> str:
    return f"tenant_{key.tenant_id}:ticket_{key.chat_id}:{key.sender_id}"

# 注入到 RelayApp
relay = RelayApp(agent, session_resolver=shared_team_resolver)
```

---

## 3. 开箱即用隔离标准 (Default Topology)

如果不显式传入 `session_resolver`，`SessionManager` 采用如下默认拓扑：
- **1 对 1 单聊 (`is_direct_message=True`)**：
  整个单聊是一个连续的会话：`{platform}:{chat_id}`；
- **群聊帖子 / Thread (`thread_id` 存在)**：
  在帖子内按发送人隔离：`{platform}:{chat_id}:{thread_id}:{sender_id}`；
- **普通大群公开聊天**：
  在群内按发送人隔离：`{platform}:{chat_id}:{sender_id}`。

这样从根本上杜绝了群聊里 A 问完天气，B 紧接着问“我刚才说了什么”，机器人却回答了 A 刚才私密内容的安全漏洞。

---

## 4. 25 小时空闲超时与自动闭环 (25h Idle TTL)

在 IM 平台上，用户不会主动点击“新建对话”。如果上下文无限追加，Token 窗口会迅速耗尽且费用飙升。

### 规则
1. **滑动窗口刷新**：每次用户与 Agent 产生交互，会刷新该会话记录的 `last_active_at`；
2. **25 小时自然跨天失效**：
   - 如果用户在 25 小时内没有发言，下次发言时，旧会话自动归档；
   - 系统生成全新的 `agno_session_id`，Agent 重新开启新鲜上下文；
   - 25 小时（而不是 24 小时）的设计巧妙避开了用户每天固定时间打卡使用导致的边界截断。
3. **主动重置命令**：用户随时发送 `/reset`、`/new`、`/clear`，网关会调用 `close_session()` 立即关闭当前会话并发送系统确认。

---

## 5. 持久化存储后端

- **`InMemorySessionStore`**：默认内置，纯内存字典，适合本地测试与单机调试；
- **`SQLiteSessionStore`**：基于 `aiosqlite` 的嵌入式持久化存储，进程重启后自动恢复会话记录：
  ```python
  from agno_harness.sessions import SessionManager, SQLiteSessionStore

  store = SQLiteSessionStore(db_path="/data/sessions.db")
  session_manager = SessionManager(store=store)
  relay = RelayApp(agent, session_manager=session_manager)
  ```
