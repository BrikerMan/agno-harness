# HITL

流正常结束、等人。不是 interrupt，也没有单独的 HITL 事件类型。

协议与答案 JSON 在这篇。产品壳其它铁律仍看 [frontend-ui.md](frontend-ui.md)；用户切到非 Agent 业务页或离开浏览器时的待办唤回通知见 [frontend-ui.md §1.2 D](frontend-ui.md#d-跨页面与离开时的-hitl-待办提醒机制)。后端声明工具看 [agents.md](agents.md)。Demo 场景：`HITL: confirmation` / `user input` / `frontend tool` / `user feedback`。

---

## 协议

暂停那一轮已经 `RUN_FINISHED`。`isStreaming === false`。等人的是表单，不是还在吐 token 的流。

```
CUSTOM run.paused          # 每种等待一个；先于 completion 里的 TOOL_CALL_*
TOOL_CALL_START / ARGS / END
RUN_FINISHED
```

`translator.complete()` 先 yield `run.paused`，再吐 completion 里的 `TOOL_CALL_*`。前端看到 START 时，若该 id 已在 `pendingTools` 里，卡片直接 `waiting`，不要先画成 `running`。

`run.paused` value：

```json
{
  "pauseType": "confirmation",
  "toolCallId": "…",
  "toolName": "delete_file",
  "toolArgs": { "path": "/tmp/old-report.csv" },
  "userInputSchema": null
}
```

`user_input` / `user_feedback` 才带 `userInputSchema`。表单只从这里长，不要猜工具名。`user_input` 的 `name` 是字段名；`user_feedback` 的 `name` 是问题原文（resume 的 `selections` key），另有 `options`（label）和 `multiSelect`。

四种暂停，同一种恢复：再 `POST /agui`，messages **末尾** `role: tool`（`detect_resume` 只认 trailing）。不要新开 user 气泡。答案 JSON 走 Agno AG-UI resume（`resolve_requirements_from_tool_messages`），不是 Python 里的 `requirement.confirm()` / `continue_run()`。

| `pauseType` | Agno | 答案 `content` | 含义 |
| --- | --- | --- | --- |
| `confirmation` | `requires_confirmation=True` | `{"accepted": true}` / `{"accepted": false, "note": "…"}` | 这调用能不能跑。Deny 后工具不执行 |
| `user_input` | `requires_user_input=True` | `{"values": {"fieldName": …}}` | 缺字段，补完再跑 |
| `user_feedback` | `user_feedback_schema` | `{"selections": {"问题原文": ["label", …]}}` | 选项。key 是 question 原文，value 必须是 label 数组 |
| `external_execution` | `external_execution=True` | 原始结果，或 `{"error": "…"}` | 浏览器 / 设备执行 |

这就是标准记录。不要把 confirmation 收成 question + true/false：`values` 里的 false 会被当成参数，工具照跑。

---

## 前端

抄 [`PendingToolPanel.tsx`](../examples/demo/frontend/src/components/hitl/PendingToolPanel.tsx)、[`use-agui-chat.ts`](../examples/demo/frontend/src/hooks/use-agui-chat.ts)、[`ToolCallCard.tsx`](../examples/demo/frontend/src/components/chat/ToolCallCard.tsx)。

### 交互

- `pendingTools` 有东西：画表单，**禁用 composer 和场景**（流已经结束，只禁 `isStreaming` 会让用户再发一条把确认冲掉）。
- confirmation：Allow / Deny → `{accepted}`（可选 `note`）。
- user input：按 `userInputSchema` 出输入框 → `{values}`（key 是字段名）。
- user feedback：按 `userInputSchema` 出选项 → `{selections}`（key 是问题原文，value 是 label 数组）。
- frontend tool：`external_execution` 自动跑（`useRef` 防 StrictMode 跑两次），结果当 tool message 回去。
- 同一 `toolCallId` 只补丁，不复制卡片。Resume 会再播一遍 `TOOL_CALL_*`。
- 提交当时就 `stampToolAnswers`，卡片立刻能看见答案，不等第二轮帧。

### 刷新

- paused run 在 `/active`，免心跳。从 frames 还原 `pendingTools`，**不要** live-tail 已结束的 paused run。
- 历史 `run.paused` 在后来的 `RUN_STARTED` / `TOOL_CALL_RESULT` 之后必须收掉表单。`/active` 没有 paused run 时也收。
- 用户问句不在 frames 里，从 `/messages` interleave；HITL resume 会再写一遍同一句，连续重复的 user 气泡去重。

**错法：** 把 HITL 当 interrupt；答案塞进新 user 消息；confirmation 改发 `{values: {accepted}}`；刷新只信 `/active` 有没有 schema。

---

## 卡片上怎么看见答案

`ToolCallCard` 读 tool 的 `args` / `result` JSON，摊成字段：

| 来源 | 看见 |
| --- | --- |
| `accepted` | **DECISION** Allowed / Denied |
| user_input `values` | 字段名（RECIPIENT / MESSAGE） |
| user_feedback `selections` | 问题原文 → 选中的 label |
| frontend tool 结果 | ERROR / lat / lng |

直播：`stampToolAnswers` 把答案 merge 进 `args`（object merge，resume 再来的 `{path}` 不会把 `accepted` 冲掉）。

回放：优先走 resume 落下的 `TOOL_CALL_RESULT`。没有时 demo 才从 `/messages` 补：有 `result` 就缝回去；confirmation 且 session 是 `error` + 空 result → Denied。这是补洞，不是协议。

---

## 落盘

resume 时 runtime 把 trailing ToolMessage 原样落成 `TOOL_CALL_RESULT`（同一 `toolCallId`），回放走这条 RESULT。

Agno 仍可能把 Deny 收成 session `status: error`、`result: ""`。刷新时 demo 还会用 `/messages` 补洞；有 RESULT 帧就不必猜。

---

## Demo 怎么点

`make run`（或先 `make run-be` 再 `make run-fe`）→ 左栏 Human in the loop。

1. **HITL: user input** — 「Send a message for me.」填 recipient / message → Submit。卡片应立刻有字段；表单消失；不要第二条用户气泡。
2. **HITL: confirmation** — 「Delete the file /tmp/old-report.csv」→ Allow 或 Deny。卡片 **DECISION Allowed/Denied** + PATH；按钮消失。模型有时只说话不调工具，再发一句让它 `delete_file`。Deny 之后它可能再调一次，再拒一次即可。
3. **HITL: frontend tool** — 「Where am I？」浏览器自动跑 `get_browser_location`。无定位时卡片 **ERROR**（超时在自动化里是预期）。
4. **HITL: user feedback** — 「Ask me which tone… Formal / Casual / Terse」→ 点选项 → Submit。卡片立刻有选中的 label；表单消失。模型走 `ask_user`，有时只说话不调工具，再发一句让它问。
