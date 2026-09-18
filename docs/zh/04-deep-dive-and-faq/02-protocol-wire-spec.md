# AG-UI 线级通信协议规范（Wire Protocol Spec）

本文档详细定义了 `agno-harness` 执行引擎向传输层（Web SSE、WebSocket、调试 Tap）输出的完整线级事件帧结构与生命周期时序。

无论你是在编写自定义前端客户端、中间件代理，还是 AI coding agent 进行协议逆向分析，本规范均为权威事实源。

---

## 1. 会话运行生命周期时序

一个标准的 Agent Turn 执行必须严格遵守以下单向状态机时序：

```
[RUN_STARTED]
      │
      ├─► [REASONING_MESSAGE_START] ──► [REASONING_MESSAGE_CONTENT]* ──► [REASONING_MESSAGE_END]
      │
      ├─► [TOOL_CALL_STARTED] ──► [TOOL_CALL_ARGS]* ──► [TOOL_CALL_COMPLETED]
      │
      ├─► [TEXT_MESSAGE_START] ──► [TEXT_MESSAGE_CONTENT]* ──► [TEXT_MESSAGE_END]
      │
      ├─► [CUSTOM (ui.block.start | ui.item | ui.block.end)]*
      │
      ├─► [CUSTOM (run.paused)] (可选：当触发 HITL 时暂停并等待)
      │
      ▼
[RUN_FINISHED] (或 AgentRunFailed 异常帧)
```

---

## 2. 核心事件帧（Wire Frames）结构定义

每个事件在 SSE 流中格式为：
```http
event: <event_name>
data: <json_string>

```

### 2.1 RUN_STARTED（运行启动）
```json
{
  "event": "run_started",
  "thread_id": "teams:chat_123:user_456",
  "run_id": "run-a1b2c3d4",
  "metadata": {
    "platform": "web",
    "agent_name": "DevOps Assistant"
  }
}
```

### 2.2 TEXT_MESSAGE_CONTENT（文本打字机分片）
```json
{
  "event": "text_message_content",
  "message_id": "msg-987",
  "delta": "你好！正在为您查询"
}
```

### 2.3 REASONING_MESSAGE_CONTENT（思考链分片）
针对 DeepSeek-R1、o1 等思考模型，思考过程与正式回答严格物理隔离：
```json
{
  "event": "reasoning_message_content",
  "message_id": "msg-987",
  "delta": "用户想要查询昨天的生产错误日志，先调用日志检索工具..."
}
```

### 2.4 TOOL_CALL_STARTED & TOOL_CALL_COMPLETED（工具调用与返回）
```json
// 启动调用
{
  "event": "tool_call_started",
  "tool_call_id": "call_12345",
  "tool_name": "query_logs",
  "args": {"service": "payment", "level": "ERROR"}
}

// 完成并返回结果
{
  "event": "tool_call_completed",
  "tool_call_id": "call_12345",
  "tool_name": "query_logs",
  "result": {"count": 3, "logs": ["Connection timeout to DB"]}
}
```

### 2.5 CUSTOM: run.paused（人机审批挂起）
```json
{
  "event": "custom",
  "name": "run.paused",
  "value": {
    "action_id": "agno.hitl.resume",
    "tool_call_id": "call_999",
    "tool_name": "drop_temp_tables",
    "pause_type": "confirmation",
    "tool_args": {"database": "prod_analytics"},
    "session_id": "teams:chat_1:user_1"
  }
}
```

### 2.6 CUSTOM: ui.block / ui.item（自适应卡片）
```json
// 开始一个卡片容器
{
  "event": "custom",
  "name": "ui.block.start",
  "value": {"name": "movie-list", "props": {"title": "推荐电影"}}
}

// 插入一个解析后的卡片条目
{
  "event": "custom",
  "name": "ui.item",
  "value": {
    "name": "movie",
    "data": {"id": 101},
    "resolved": {"title": "星际穿越", "rating": 9.4, "year": 2014}
  }
}

// 结束卡片容器
{
  "event": "custom",
  "name": "ui.block.end",
  "value": {}
}
```

### 2.7 RUN_FINISHED（运行结算）
```json
{
  "event": "run_finished",
  "thread_id": "teams:chat_123:user_456",
  "run_id": "run-a1b2c3d4",
  "status": "completed"
}
```

---

## 3. 错误与中断帧保护

若模型运行期间发生未捕获异常或用户主动点击取消：
1. **取消中断**：发出 `EVENT_RUN_CANCELLED` 帧，随后自动对底层悬挂中的 Tool Call 进行闭环修补（`close_dangling_tool_calls`），防止数据库持久化损坏；
2. **异常保护**：发出包装后的 `AgentRunFailed` 帧，并在底层调用 `channel.settle(emoji="❌")` 向聊天窗口同步反馈错误信息。
