# 02. 侧栏 UI：与 Chat 解耦

Todo 是**全局当前状态**，Chat 是**时序日志**。10–20 步若每次在气泡里出卡，用户看不到关键回复。

## 1. 为什么拆开

1. 长流程会刷屏，视线应停在专门的 Todo 区。
2. 气泡是历史；清单是「现在做到哪」。塞进旧气泡会被顶出可视区。
3. 等 Bash / 分析时，聊天区只留高价值文字。

## 2. 两种形态

```text
A 右侧常驻                              B 浮层
┌────────┬──────────┬────────┐         ┌────────┬──────────────────┐
│ 会话   │  主对话   │ Todo   │         │ 会话   │ 主对话            │
│        │  文字/工具 │ 进度   │         │        │ Tasks: 3/5 [开]  │
│        │          │ 树     │         │        │ ┌──────────────┐ │
└────────┴──────────┴────────┘         │        │ │ 弹窗任务树   │ │
桌面 / IDE / 长任务工作台               └────────┴─┴──────────────┴─┘
                                       窄屏 / 移动
```

- **A：** `w-72`–`w-80`，与对话平级，余光看进度。
- **B：** 聊天里一行 `Tasks: 3/5 (60%)`，点开 Dialog；Esc / 遮罩关闭。

从最新消息倒序找激活块，挂到**聊天外面**：

```tsx
const activeTodoBlock = useMemo(() => {
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    const block = chat.messages[i].uiBlocks?.find((b) => b.schema === "todo-list");
    if (block && block.items.length > 0) return block;
  }
  return null;
}, [chat.messages]);

return (
  <div className="flex h-screen overflow-hidden">
    <aside className="w-64 border-r">{/* 会话列表 */}</aside>
    <main className="flex-1 flex flex-col">
      {activeTodoBlock && (
        <button onClick={() => setTodoOpen(true)}>
          Tasks: {stats.done}/{stats.total}
        </button>
      )}
      <MessageList messages={chat.messages} />
      <Composer />
    </main>
    <TodoPanel
      block={activeTodoBlock}
      open={todoOpen && activeTodoBlock !== null}
      onClose={() => setTodoOpen(false)}
      isStreaming={chat.isStreaming}
      mode="sidebar"
    />
  </div>
);
```

气泡里对 `todo_write` 返回 `null`（或服务端 `HideToolFilter`）。`todo-list` 放进 `REPLACING_SCHEMAS`，原地换，不要每轮往下刷一张。

```tsx
export function TodoPanel({ block, open, onClose, isStreaming, mode = "sidebar" }: TodoPanelProps) {
  const [displayMode, setDisplayMode] = useState(mode);
  if (!open || !block) return null;
  const content = (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b p-3">
        <h3>{title}</h3>
        <button onClick={() => setDisplayMode((m) => (m === "sidebar" ? "popup" : "sidebar"))} />
        <button onClick={onClose} />
      </div>
      <ProgressBar percent={stats.percent} />
      <div className="flex-1 overflow-y-auto p-3">{/* 树 */}</div>
    </div>
  );
  if (displayMode === "popup") {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
        <div className="max-h-[85vh] max-w-lg overflow-hidden rounded-xl border bg-background" onClick={(e) => e.stopPropagation()}>
          {content}
        </div>
      </div>
    );
  }
  return <aside className="flex w-80 shrink-0 flex-col border-l">{content}</aside>;
}
```

## 3. 防空转（Web 对了才算对）

**边界 A：** 结论写完但漏了最后一次打勾 → 进度挂 90%。  
**边界 B：** 用户 Stop / 断流 → 必须保留现场，禁止全标完成。

Toolkit Prompt 已写死终态铁律（[01](01-toolkit.md)）。前端**只**在同时满足时把末项兜成 `done`：

- `!isStreaming`
- 前面全部 `done`（无 `pending` / `error`）
- **只有**最后一项是 `doing` 或 `pending`

```tsx
if (!isStreaming && parsed.length > 0) {
  const last = parsed.length - 1;
  const allPriorDone = parsed.slice(0, last).every((it) => it.state === "done");
  if (allPriorDone && (parsed[last].state === "doing" || parsed[last].state === "pending")) {
    return parsed.map((item, idx) =>
      idx === last ? { ...item, state: "done" as const } : item,
    );
  }
}
```

中途停（前面还有 `pending`，或用户 Stop）：

- **不要**全部标完成。5 步做了 2 步就停在 40%。
- `doing` 去掉 `animate-spin`，改静止灰（`PauseCircle` + 徽章 `Stopped`）。
- 进度条颜色从活跃蓝改成灰。

## 4. 层级序号

禁止 `idx + 1`。父任务中间插子步骤会变成 `#4`。模型标签里已有 `1.` / `2.1` 时不要再拼一层。

```tsx
let parentNum = 0;
let subNum = 0;
const parsed = block.items.map((item) => {
  const rawLabel = String((item.data as Record<string, unknown>)?.label ?? "").trim();
  const level = Number((item.data as Record<string, unknown>)?.level ?? 0);
  const numMatch = rawLabel.match(/^(\d+(?:\.\d+)*)[.)\-:\s]\s*(.*)$/);
  if (level === 0) {
    parentNum += 1;
    subNum = 0;
    return { displayIndex: numMatch ? numMatch[1] : String(parentNum), label: numMatch?.[2] || rawLabel, level };
  }
  subNum += 1;
  return { displayIndex: numMatch ? numMatch[1] : `${parentNum}.${subNum}`, label: numMatch?.[2] || rawLabel, level };
});
```

顶层 `#1`，子步 `↳ 2.1`。

## 5. Web 怎么确定没问题

按这个顺序点，全绿才算侧栏做对：

| # | 操作 | 必须看到 |
| --- | --- | --- |
| 1 | 让 Agent 规划 4 步并执行 | 右侧（或顶栏 `Tasks: n/m`）出清单；**聊天气泡里没有** `todo_write` 大段参数 |
| 2 | 跑到一半刷新 | 同一套 `applyEvent` 回放后，勾选停在刷新前那一格，不是空列表、不是重算乱序 |
| 3 | 模型写完结论但漏打勾 | `!isStreaming` 后末项自动变 `done`，进度到 100%，Spinner 停 |
| 4 | 5 步做到第 3 步点 Stop | 进度停在真实比例；第 3 步 `Stopped` 灰标；第 4–5 仍 `pending`；**没有**全绿勾 |
| 5 | `allow_subtasks=True`，父 3 + 子步 | 父是 `#1 #2 #3`，不是 `#1 #3 #5`；已带 `2.1` 的标签不再变成 `#2.1 2.1` |
| 6 | 再调一次 `todo_write` | 侧栏**原地换**，聊天区不往下多一张 Todo 卡 |

回放与 FAQ：[03](03-replay-and-faq.md)。
