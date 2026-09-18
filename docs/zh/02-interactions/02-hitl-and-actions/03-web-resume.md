# 03. Web：表单、刷新、卡片上的答案

产品壳铁律仍看 [Web 01](../../03-clients/01-web-react/01-iron-rules.md)。本页只写暂停卡怎么续上。

## 交互

- `pendingTools` 有东西：画表单，**禁用 Composer 和场景切换**。流已经结束，只禁 `isStreaming` 会让用户再发一条把确认冲掉。
- confirmation：Allow / Deny → `{accepted}`（可选 `note`）。
- user input：按 `userInputSchema` → `{values}`（key 是字段名）。
- user feedback：选项 → `{selections}`（key 是问题原文，value 是 label 数组）。
- frontend tool：`external_execution` 自动跑（`useRef` 防 StrictMode 两次），结果当 tool message。
- 同一 `toolCallId` 只补丁。Resume 会再播一遍 `TOOL_CALL_*`。
- 提交当时就 `stampToolAnswers`，卡片立刻能看见答案，不等第二轮帧。

```ts
await send({
  toolResults: [{
    toolCallId: pendingTool.toolCallId,
    content: JSON.stringify({ accepted: true }),
  }],
});
```

## 刷新

- paused run 在 `/active`，免心跳。从 frames 还原 `pendingTools`，**不要** live-tail 已结束的 paused run。
- 历史 `run.paused` 在后来的 `RUN_STARTED` / `TOOL_CALL_RESULT` 之后必须收掉表单。`/active` 没有 paused 时也收。
- 用户问句优先信 frames 里 `RUN_STARTED.user_input`；HITL resume 会再写同一句，连续重复的 user 气泡去重。

**错法：** 刷新只信 `/active` 有没有 schema；答案塞进新 user 消息。

## 卡片上怎么看见答案

| 来源 | 看见 |
| --- | --- |
| `accepted` | **DECISION** Allowed / Denied |
| user_input `values` | 字段名（RECIPIENT / MESSAGE） |
| user_feedback `selections` | 问题原文 → 选中的 label |
| frontend tool | ERROR / 结果字段 |

直播：`stampToolAnswers` 把答案 merge 进 `args`（object merge，resume 再来的 `{path}` 不会冲掉 `accepted`）。

回放：优先 `TOOL_CALL_RESULT`（runtime 把 trailing ToolMessage 落成同一 `toolCallId`）。没有 RESULT 时不要猜 session `status: error` + 空 result —— 那是 Agno 收 Deny 的副作用，不是协议。

跨页 Banner / 桌面通知见 [Web 02](../../03-clients/01-web-react/02-thread-shell.md)。
