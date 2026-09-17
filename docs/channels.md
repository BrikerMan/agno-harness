# 多渠道接入与流控机制 (Channels & Stream Modes)

`agno-relay` 提供了从本地终端到企业 IM 的全套通道适配器。

---

## 1. 为什么必须区分流模式 (Stream Modes)？

不同渠道对流式输出的容忍度完全不同：
- **Web / CLI**：逐字 SSE 流式打字机是最佳体验；
- **Teams / 飞书**：如果对每 5 个 token 调一次 `patch_message`，**3 秒内就会触发 HTTP 429 被平台封禁**！

`agno-relay` 提供了三种模式：

| 模式 | 适用渠道 | 工作方式 |
| :--- | :--- | :--- |
| `StreamMode.RAW` | Web (AG-UI), CLI | 零延迟原始分块实时推送。 |
| `StreamMode.FINAL` (推荐默认) | Teams, 飞书/Lark | 收到消息**立刻回 Reaction (🤔) 并维持后台 Typing 心跳**，Agent 执行完毕后一次性发出完整 Markdown 与聚合卡片，最后打勾 (✅)。**彻底免疫 429**。 |
| `StreamMode.THROTTLE` | Teams, 飞书/Lark | 1.5 秒自适应滑动窗口缓冲合并更新。 |

---

## 2. 渠道详解

### 2.1 WebChannel (AG-UI SSE)
基于 FastAPI 导出端点，提供：
- `POST /agui`: 遵循标准 AG-UI 协议；
- `GET /runs/{id}/attach`: 0s 首次 keepalive ping，支持意外断网重连；
- 配合前端 `@ag-ui/react` 或自定义 UI 即开即用。

```python
from agno_relay import WebChannel, AguiRuntime
from fastapi import FastAPI

app = FastAPI()
web = WebChannel(runtime=my_runtime)
app.include_router(web.get_router(), prefix="/api")
```

### 2.2 CLIChannel (Rich 交互控制台)
用于极速本地调试与测试。支持格式化 Markdown 输出与 Card 面板展示：
```python
from agno_relay import CLIChannel

cli = CLIChannel()
# 在 RelayApp 中挂载后即可在终端直接交互输入
```

### 2.3 LarkChannel (飞书长连接)
支持 **WebSocket 免公网 IP 模式**，本地开发无需 ngrok，直接一条长连接连入飞书开放平台网关：
```python
from agno_relay import LarkChannel

lark = LarkChannel(
    app_id="cli_xxx",
    app_secret="xxx",
    use_websocket=True, # 零公网配置
)
```

### 2.4 TeamsChannel (微软 Teams)
基于微软最新 Microsoft 365 Agents SDK 构建：
```python
from agno_relay import TeamsChannel

teams = TeamsChannel(
    bot_app_id="xxx",
    bot_app_password="xxx",
)
fastapi_app.include_router(teams.get_router()) # 暴露 /api/messages
```
