# 02. Sidebar UI: decoupled from chat

A todo list is **live global state**. Chat is a **timestamped log**. Ten to twenty steps as bubbles hide the answer.

## 1. Why split it out

1. A long run floods the transcript. The eye should sit on a dedicated todo surface.
2. A bubble is history; the list is “where we are now”. Buried in an old bubble it scrolls away.
3. While a script or analysis runs, chat keeps only high-value prose.

## 2. Two layouts

```text
A persistent rail                         B popup
┌────────┬──────────┬────────┐           ┌────────┬──────────────────┐
│ threads│  chat    │ Todo   │           │ threads│ chat             │
│        │  text    │ bar    │           │        │ Tasks: 3/5 [open]│
│        │  tools   │ tree   │           │        │ ┌──────────────┐ │
└────────┴──────────┴────────┘           │        │ │ modal tree   │ │
desktop / IDE / long jobs                └────────┴─┴──────────────┴─┘
                                         narrow / mobile
```

- **A:** `w-72`–`w-80`, peer to the transcript, glanceable.
- **B:** one row `Tasks: 3/5 (60%)` in chat; open a dialog; Esc / backdrop closes it.

Find the active block from the newest message, mount it **outside** chat:

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
    <aside className="w-64 border-r">{/* thread list */}</aside>
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

Return `null` for `todo_write` in the bubble (or `HideToolFilter` on the server). Put `todo-list` in `REPLACING_SCHEMAS` and swap in place.

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
      <div className="flex-1 overflow-y-auto p-3">{/* tree */}</div>
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

## 3. Anti-spin (the Web is wrong until these pass)

**Edge A:** the final answer landed but the last check-off was skipped → bar stuck at 90%.  
**Edge B:** user Stop / drop → keep the scene; never mark everything done.

The toolkit prompt already hard-requires a final `todo_write` ([01](01-toolkit.md)). The client may mark the last item `done` **only** when all of these hold:

- `!isStreaming`
- every earlier item is `done` (no `pending` / `error`)
- **only** the last item is `doing` or `pending`

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

Mid-stop (earlier `pending` remains, or the user hit Stop):

- **Do not** mark everything done. Two of five steps → bar at 40%.
- Drop `animate-spin` on `doing`; quiet grey (`PauseCircle` + `Stopped`).
- Progress bar color goes from live blue to grey.

## 4. Hierarchical index

Do not use `idx + 1`. A child under a parent turns `#3` into `#4`. If the model already wrote `1.` / `2.1`, do not prefix again.

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

Parents `#1`, children `↳ 2.1`.

## 5. How you know the Web is right

Walk this table. All green means the sidebar is done:

| # | Do this | You must see |
| --- | --- | --- |
| 1 | Ask for a 4-step plan and run it | The rail (or `Tasks: n/m`) shows the list; **no** raw `todo_write` dump in the bubble |
| 2 | Refresh mid-run | Same `applyEvent` replay; checks sit where they were — not empty, not re-sorted |
| 3 | Model finishes prose but skips the last check | After `!isStreaming` the last item becomes `done`, bar hits 100%, spinner dies |
| 4 | Stop on step 3 of 5 | Bar stays at the real ratio; step 3 is grey `Stopped`; 4–5 stay `pending`; **not** all green |
| 5 | `allow_subtasks=True`, 3 parents + children | Parents are `#1 #2 #3`, not `#1 #3 #5`; a label that already has `2.1` is not `#2.1 2.1` |
| 6 | A second `todo_write` | The sidebar **replaces in place**; chat does not grow another todo card |

Replay and FAQ: [03](03-replay-and-faq.md).
