# 02-interactions / 发给模型的用户请求（User Query Envelope）

模型没有钟。系统提示里写一句「今天是周五」不够：那是整段会话共用的一个日期，隔了几小时会飘，压缩还可能把它挤掉。

**在交给 `agent.arun(input=)` 之前**，把这一轮包成两块：`<user-query>` 只有用户原话；时间、发送人、渠道放进 `<context>`。时间跟着这一轮进 Agno 历史，后面问「下午那件事」才答得上。前端只要取 `<user-query>` 的 inner text，不用再拆括号。

`UserQueryBuilder` 默认开。

---

## 为什么时间跟着这一轮走，却不写进用户原话

1. **相对时间是相对「这一条」的。** 「今天下午开会」锚定的是消息自己的墙钟，不是会话开始的那天。写在这一轮的 `<context>` 里，压缩后还在。
2. **系统提示只有一个「今天」。** 隔夜续聊，一条 `today is …` 会骗模型。
3. **前端 / Langfuse 要看原话。** 时间、发送人若拼进问句，UI 得再解析。原话单独放 `<user-query>`，元数据用标签放 `<context>`。

默认格式：

```text
<user-query>
今天下午开会吗
</user-query>

<context note="Reference only. ...">
  <time>2026-09-18 13:32:00 +0800</time>
  <user>eliyar</user>
  <platform>lark</platform>
  <source-type>dm</source-type>
</context>
```

时区只认 `AGNO_HARNESS_TIMEZONE`（IANA，例如 `Asia/Shanghai`）。没设就用进程本地时区。偏移写在 `<time>` 里。

---

## 默认行为

`AgentRuntime` 默认 `enable_user_query=True`。Relay 把 `sent_at` / `sender_name` / `platform` / `is_direct_message` / 附件提取写进 `metadata`；Web 没有渠道时间时，用请求到达的此刻。

已经是 `<user-query>` 开头的内容不会再包一层。HITL 恢复不走这层。

```python
runtime = AgentRuntime(agent=agent, enable_user_query=False)
```

---

## 自己加上下文插件

```python
from agno_harness import QueryTurn, UserQueryBuilder
from agno_harness.core.prompt import SourceContextPlugin

class TenantPlugin:
    name = "tenant"

    async def contribute(self, turn: QueryTurn, ctx: dict) -> None:
        tenant = turn.extras.get("tenant_id")
        if tenant:
            ctx["tenant"] = tenant

runtime = AgentRuntime(
    agent=agent,
    user_query_builder=UserQueryBuilder(
        plugins=[SourceContextPlugin(), TenantPlugin()],
        timezone="Asia/Shanghai",
    ),
)
```

插件只往 `<context>` 里塞键。不要改用户原话。
