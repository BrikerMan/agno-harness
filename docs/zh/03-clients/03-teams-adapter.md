# 03-clients / Microsoft Teams 适配

`TeamsChannel()` 读取 `AGNO_HARNESS_TEAMS_*`，按 Bot Framework 收发。可复制运行的服务器在 [Teams 菜谱](../00-agent-cookbook/03-teams-bot-agent.md)。这一页是那份服务器背后的约定。

---

## 1. 快速接入

```python
from fastapi import FastAPI
from agno.agent import Agent
from agno_harness import AgentRuntime, RelayApp, TeamsChannel

runtime = AgentRuntime(agent=Agent(name="Teams Assistant", instructions="你是办公助手。"))
relay = RelayApp(runtime=runtime)
relay.add_channel(TeamsChannel())  # 或 TeamsChannel(bot_app_id="...", bot_app_password="...", tenant_id="...")

app = FastAPI(lifespan=relay.lifespan)
app.include_router(relay.get_router(allow_anonymous=True))
```

`RelayApp` 要的是 `AgentRuntime`，不是裸 `Agent`。租户参数是 `tenant_id`（`bot_tenant_id` 仍可作为别名）。

Webhook 是 `POST /api/v1/channels/teams/messages`。router 的 `prefix` 会加在前面，所以 `prefix="/agent"` 时 Azure Messaging endpoint 是 `POST /agent/api/v1/channels/teams/messages`。

`agno-harness teams doctor` 会打印这个地址，检查环境变量，并申请一次 connector token。

---

## 2. 零参数 `TeamsChannel()` 实际做什么

- **JWT。** `Authorization: Bearer` 用 Bot Framework JWKS 校验，audience 是 App ID。没有 token 或 token 无效返回 HTTP 401。缺 App ID 或密码返回 HTTP 503，不会假装 200。
- **回帖。** 入站 `serviceUrl` 和 conversation id 会缓存。`send()` 用 client credentials token 发 message activity，返回 connector 的 activity id，不会编造 `teams_msg_...`。
- **serviceUrl。** 只允许 `https`，且主机必须落在 `*.botframework.com`、`*.botframework.azure.us`、`*.smba.trafficmanager.net`。其他主机在发出请求前抛错。
- **Typing。** `ack()` 和 `typing(active=True)` 发送 `typing` activity。Connector 不支持任意 emoji reaction，所以 `settle()` 不会往用户消息上盖 👀/✅/❌。用户看到的结果是那条回复。
- **Activity。** `message` 和 `invoke`（Adaptive Card 按钮）会变成 `ChannelEvent`。`conversationUpdate` 以及其他类型只打日志。invoke 会返回 invoke response，避免 Teams 显示失败。
- **Mention-only。** 群聊和频道默认 `MentionOnlyPolicy`，除非传入 `chime_in_policy`。私聊仍然回复。CLI 不变。`RelayApp(chime_in_policy=...)` 会覆盖渠道策略。
- **话题。** 频道话题回复用 `conversation.id` 里的 `messageid` 做会话线程。频道根消息用 `teamsChannelId`。不用 `teamsTeamId`，同一个团队里的两个频道不会串上下文。

---

## 3. 租户与 token 权威

| `AGNO_HARNESS_TEAMS_TENANT_ID` | Token URL |
|---|---|
| 已填写 | `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` |
| 留空 | `https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token` |

单租户必须填 tenant。多租户留空。scope 是 `https://api.botframework.com/.default`。

---

## 4. 流模式

Teams 不要用 `StreamMode.RAW`。默认 `final` 等 Agent 结束后发一条。`throttle` 至少按 1.5 秒窗口合并编辑。

---

## 5. 附件

入站文件只保留元数据（`contentUrl`、文件名、content type），放在 `InboundAttachment` 上。Channel 不下载。需要字节或抽取文本时，给 `RelayApp` 传 `AttachmentProcessor`。

---

## 6. 安装

```bash
uv add "agno-harness[teams,fastapi]"
```

`teams` 额外依赖是 PyCryptodome，用来做 RS256 校验。出站用 `httpx`，不再依赖 Microsoft Agents SDK。
