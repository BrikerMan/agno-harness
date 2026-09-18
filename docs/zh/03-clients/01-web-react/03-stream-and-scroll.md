# 03. 事件流与跟滚

一个 reducer、只画 `order[]`、钉在底部。

## 1. SSE 与 `applyEvent`

- 不能用 `EventSource` 做 POST。自己带 carry buffer；解析 `id:` 作为 resume offset；**comment `: ping` 也要重置 idle**（见 [04](04-attach-and-longrun.md)）。
- **一个 `applyEvent`。** live SSE、`/frames` 回放、`/attach` 续写同一套。
- **只渲染 `body.order[]`。** slot 在元素打开时 push：首个 reasoning delta、`TOOL_CALL_START`、`subagent.start`、非 text 之后的首个 text delta、`ui.block.start`。
- 典型流：`reasoning → tool → subagent → 回答 text`。
- 新 text slot：`last.kind !== "text"` 就开新 key。
- 子 agent：`subAgents` 有 `running` 时，`TEXT_*` / `TOOL_CALL_*` / `REASONING_*` / `ui.*` 全部进 child body；嵌套同一套 `BodyView`。
- thinking：按 `messageId` 分块，tool 两侧两段 reasoning 不要合成一条；结束用 `endedAt` / `elapsedMs`，不要折叠后还用 `Date.now()` 跳秒。
- 持续流：逐帧 apply，最后一段 text 加 caret；未闭合 fence / 表格交给 markdown，不要等 `TEXT_MESSAGE_END`。
- `TOOL_CALL_ARGS` 是字符串增量，没 `TOOL_CALL_END` 前不要 `JSON.parse`。并行 tool 按 `toolCallId` patch。
- `toWireMessage` 只发 `{id, role, content}`。`content` 只有父 agent 正文。不要把 reasoning / 子面板塞回下一轮 messages。
- `RunAgentInput` 至少带 `threadId/runId/messages/tools/state/context/forwardedProps`。`tools: []` 和省略不是一回事。

**面板型工具不进聊天流：** `todo_write` 已有侧栏，禁止在气泡里再画 raw tool 卡。服务端 `HideToolFilter({"todo_write"})`，或客户端 `case "tool"` 时对这个名字返回 `null`。详见 [Todo](../../02-interactions/03-todo/README.md)。

**长文本卡片（Header + Meta + Content）：**

- 默认限高（如 `h-[340px]` + `overflow-y-auto`），底部淡出；展开切全高。
- 卡片内流式跟滚；用户上翻立刻停，并出「回到底部」。
- 大文件进度：`ui.item` 画页码；`ui.block.end` 的 `savedPath` / `bytes` 进 Meta。详见 [08 Artifact](../../02-interactions/08-streaming-artifacts.md)。

**错法：** 从 `/messages` 重建；把 tool 从 `order[]` 捞出来追加到末尾；按「一帧一字」写动画（archive 会把连续 content delta 合成一帧）。

## 2. 平滑跟滚

要的是：**钉在底部时内容长高就跟着走，用户上翻立刻停**。不是每 token 做一次 `scroll-behavior: smooth`。

```tsx
<div ref={stick.scrollerRef} className="flex-1 overflow-y-auto [overflow-anchor:none]">
  <div>{/* 观察这个子节点的高度 */}</div>
</div>
```

```ts
const stick = useStickToBottom();
stick.pin(); // 新发送 / 切 thread / 新对话
```

1. **容器关掉 overflow-anchor。** 浏览器锚最后一行会和 `scrollTop = scrollHeight` 对着拉。
2. **跟滚用 `ResizeObserver` 看内容高度**，钉住时 `el.scrollTop = el.scrollHeight`。不要每 token `scrollIntoView`，也不要在跟滚路径开 CSS smooth。
3. **脱离只认用户往上滚。** 上一帧 `scrollTop` 变小才 `pinned = false`。内容变高不算脱离。距底 ≤24px 重新钉住。
4. **用户主动跳到底才 pin。** 发消息、切 thread、点「新对话」调用 `pin()`。
5. **子面板自己 overflow。** 子 agent 内部滚动不要冒泡成主列表 scroll。

常见错法：跟滚用 smooth、每 token `scrollIntoView`、用「距底 > N」当脱离、子面板和主列表抢同一个 stick。

下一步：[04 刷新续写](04-attach-and-longrun.md)。
