# 03. 回放与 FAQ

`ui.block` / `ui.item` 进事件日志。刷新、`/frames`、多端看历史：同一个 `applyEvent` 重放即可。`activeTodoBlock` 回到当时的勾选，不要再解析一遍 Markdown。

中断（报错、Abort）时，存储里是最后一个 `ui.item`。回放如实停在 `doing` / `error`，不要前端「重新算一遍」抹掉现场。

live 与 replay 共用管道，见 [Web 01 铁律](../../03-clients/01-web-react/01-iron-rules.md)。

## FAQ

1. **对话结束后还卡几秒？**  
   检查 `make_thread_title_hook` 是否同步挡在这条 SSE 上。生产里标题应异步，或首轮用首句截断。

2. **进度条停在 80%–90% 一直转？**  
   模型没打最后一次 `todo_write`。确认挂了 Toolkit instructions，且前端实现了 [02](02-sidebar-ui.md) 的末步兜底。

3. **气泡里出现大段 `todo_write` 参数？**  
   面板型工具不要画 raw tool 卡。`HideToolFilter({"todo_write"})`，或 `BodyView` 对该名字返回 `null`。

4. **第三个父任务变成 `#4`？**  
   不要用数组下标。用 [02](02-sidebar-ui.md) 的 `parentNum` / `subNum`。

侧栏六条操作验收见 [02 §5](02-sidebar-ui.md)。刷新回放必须和 live 同一 `applyEvent`；中断现场不得被前端「重新算一遍」。
