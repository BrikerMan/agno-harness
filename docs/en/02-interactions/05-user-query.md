# 02-interactions / The user request the model sees (User Query Envelope)

The model has no clock. A system line that says "today is Friday" is not enough: it is one date for the whole session, it drifts after a pause, and compression can drop it.

**Before** `agent.arun(input=)`, the turn is two blocks: `<user-query>` is the user's words only; time, speaker, and channel go in `<context>`. The time travels with the turn in Agno history, so "that meeting this afternoon" still has a wall-clock. The frontend takes the inner text of `<user-query>` — no bracket parsing.

`UserQueryBuilder` is on by default.

---

## Why time is on this turn, but not inside the ask

1. **Relative time is relative to this message.** "Meeting this afternoon" anchors to this turn's clock. Putting it in this turn's `<context>` survives compression.
2. **A system prompt has one "today".** Next-day resume makes a single `today is …` lie.
3. **The UI needs the raw ask.** If time and sender are spliced into the sentence, the frontend has to parse them back out. Keep the ask in `<user-query>`; keep metadata as tags in `<context>`.

Default shape:

```text
<user-query>
are we meeting this afternoon
</user-query>

<context note="Reference only. ...">
  <time>2026-09-18 13:32:00 +0800</time>
  <user>eliyar</user>
  <platform>lark</platform>
  <source-type>dm</source-type>
</context>
```

Timezone is only `AGNO_HARNESS_TIMEZONE` (IANA, e.g. `Asia/Shanghai`). If unset, the process local zone is used. The offset is in `<time>`.

---

## Defaults

`AgentRuntime` sets `enable_user_query=True`. Relay fills `metadata` with `sent_at`, `sender_name`, `platform`, `is_direct_message`, and extracted attachments. On Web, if the channel sent no time, the request's arrival time is used.

Text that already starts with `<user-query>` is not wrapped again. HITL resume skips this path.

```python
runtime = AgentRuntime(agent=agent, enable_user_query=False)
```

---

## Add your own context plugins

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

Plugins only write into `<context>`. Do not rewrite the user's words.
