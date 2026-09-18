# 03. Stream and scroll

One reducer, paint `order[]` only, stick to the bottom.

## 1. SSE and `applyEvent`

- You cannot POST with `EventSource`. Keep a carry buffer; parse `id:` as the resume offset; **comment `: ping` must reset idle** (see [04](04-attach-and-longrun.md)).
- **One `applyEvent`.** Live SSE, `/frames` replay, and `/attach` resume share it.
- **Render only `body.order[]`.** Push a slot when the element opens: first reasoning delta, `TOOL_CALL_START`, `subagent.start`, first text delta after a non-text slot, `ui.block.start`.
- Typical stream: `reasoning → tool → subagent → answer text`.
- New text slot: open a new key when `last.kind !== "text"`.
- Sub-agents: while a child is `running`, `TEXT_*` / `TOOL_CALL_*` / `REASONING_*` / `ui.*` go into the child body. Nest the same `BodyView`.
- Thinking: split by `messageId`. Do not merge two reasoning spans across a tool. Close with `endedAt` / `elapsedMs`, not a ticking `Date.now()` after collapse.
- Apply every frame. Put a caret on the last text. Let markdown handle an open fence or table; do not wait for `TEXT_MESSAGE_END`.
- `TOOL_CALL_ARGS` is a string delta. Do not `JSON.parse` before `TOOL_CALL_END`. Patch parallel tools by `toolCallId`.
- `toWireMessage` sends `{id, role, content}` only. `content` is the parent agent body. Do not send reasoning or child panels back as next-turn messages.
- `RunAgentInput` needs at least `threadId/runId/messages/tools/state/context/forwardedProps`. `tools: []` is not the same as omitting `tools`.

**Panel tools stay out of the chat stream.** `todo_write` already has a sidebar. Do not paint a raw tool card in the bubble. Hide it with `HideToolFilter({"todo_write"})` or return `null` for that name in `case "tool"`. See [Todos](../../02-interactions/03-todo/README.md).

**Long-text cards (Header + Meta + Content):**

- Default max height (e.g. `h-[340px]` + `overflow-y-auto`) with a fade at the bottom; expand to full height.
- Follow live output inside the card; stop on user scroll-up and show “back to bottom”.
- Large-file progress: `ui.item` for pages; `savedPath` / `bytes` from `ui.block.end` go in Meta. See [08 Artifacts](../../02-interactions/08-streaming-artifacts.md).

**Wrong:** rebuild from `/messages`; pull tools out of `order[]` and append; animate “one character per frame” (the archive folds consecutive content deltas into one frame).

## 2. Stick-to-bottom

What you want: **when pinned, growing content follows; user scroll-up stops immediately.** Not a `scroll-behavior: smooth` tween on every token.

```tsx
<div ref={stick.scrollerRef} className="flex-1 overflow-y-auto [overflow-anchor:none]">
  <div>{/* observe this child's height */}</div>
</div>
```

```ts
const stick = useStickToBottom();
stick.pin(); // new send / switch thread / new chat
```

1. **Turn overflow-anchor off on the scroller.** The browser anchoring the last line fights `scrollTop = scrollHeight`.
2. **Follow with `ResizeObserver` on content height.** When pinned, `el.scrollTop = el.scrollHeight`. No per-token `scrollIntoView`, no CSS smooth on the follow path.
3. **Unpin only on user scroll-up.** `scrollTop` shrinking vs the previous frame → `pinned = false`. Growing content does not unpin. Re-pin within 24px of the bottom.
4. **Pin only on an intentional jump.** Send, switch thread, “new chat” call `pin()`.
5. **Child panels overflow themselves.** Inner sub-agent scroll must not bubble to the main list.

Common mistakes: smooth follow, per-token `scrollIntoView`, “distance from bottom > N” as unpin, one stick shared by a child panel and the main list.

Next: [04 Attach and long-run](04-attach-and-longrun.md).
