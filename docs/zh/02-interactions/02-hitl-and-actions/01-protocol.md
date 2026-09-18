# 01. HITL 协议

流正常结束、等人。不是 interrupt，也没有单独的 HITL 事件类型。

| 维度 | Agno 原生 HITL | 卡片自定义动作 |
| --- | --- | --- |
| 触发 | Agent 跑受限工具时挂起 | 用户点业务按钮 |
| 状态机 | `RunPaused`，流结束；`resume_paused_run` 唤醒 | 与 Agent 无关的 Webhook |
| 场景 | 删库确认、缺参、多选、浏览器定位 | 点赞、稍后看、外链 |
| 协议 | `CUSTOM run.paused` → 末尾 `role: "tool"` | `action_id` + payload |

## 后端声明

```python
from agno.tools import tool

@tool(requires_confirmation=True)
async def delete_file(path: str) -> str: ...

@tool(requires_user_input=True, user_input_schema=[
    {"name": "recipient", "description": "收件人邮箱", "fieldType": "string", "required": True},
])
async def send_email(recipient: str, subject: str, body: str) -> str: ...

@tool(user_feedback_schema=[{
    "name": "选择部署环境",
    "fieldType": "choice",
    "options": ["staging", "production"],
    "multiSelect": False,
}])
async def deploy_service(environment: str) -> str: ...
```

`external_execution` 通过 `RunAgentInput.tools` 声明，浏览器执行。`user_feedback` 挂 Agno `UserFeedbackTools`，模型调 `ask_user`。

## 暂停帧

`translator.complete()` 先 yield `run.paused`，再吐 `TOOL_CALL_*`，最后 `RUN_FINISHED`。`isStreaming === false`。前端看到 START 时若该 id 已在 `pendingTools`，卡片直接 `waiting`，不要先画 `running`。

```json
{
  "pauseType": "confirmation",
  "toolCallId": "…",
  "toolName": "delete_file",
  "toolArgs": { "path": "/tmp/old-report.csv" },
  "userInputSchema": null
}
```

`user_input` / `user_feedback` 才带 `userInputSchema`。表单只从这里长。`user_input` 的 `name` 是字段名；`user_feedback` 的 `name` 是问题原文（resume 的 `selections` key），另有 `options` 与 `multiSelect`。

## 同一种恢复

再 `POST /agui`，messages **末尾** `role: tool`（`detect_resume` 只认 trailing）。不要新开 user 气泡。答案走 Agno AG-UI resume，不是 Python 里的 `requirement.confirm()`。

| `pauseType` | 答案 `content` |
| --- | --- |
| `confirmation` | `{"accepted": true}` / `{"accepted": false, "note": "…"}`。Deny 后工具不执行 |
| `user_input` | `{"values": {"fieldName": …}}` |
| `user_feedback` | `{"selections": {"问题原文": ["label", …]}}`。key 是问题原文，value 必须是 label 数组 |
| `external_execution` | 原始结果，或 `{"error": "…"}` |

**反模式：**

1. 把 HITL 当 interrupt；
2. 答案当成新的 `user` 消息；
3. confirmation 写成 `{"values": {"accepted": false}}` —— `values` 里的 false 会被当成参数，工具照跑。

下一步：[02 IM 与 Class 动作](02-im-and-class-actions.md)。
