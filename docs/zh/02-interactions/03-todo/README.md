# 02-interactions / Todo

多步骤任务要单独一块进度面，不要和聊天气泡挤在一起。长文档去 [08 Artifact](../08-streaming-artifacts.md)。

Harness 在 `todo_write` 执行时清洗 Markdown，发射 `ui.block` / `ui.item`。前端只消费结构化块，不解析半截 JSON。

| 步 | 内容 |
| --- | --- |
| [01 Toolkit](01-toolkit.md) | `TodoToolkit`、`allow_subtasks`、状态、IM 勾选 |
| [02 侧栏 UI](02-sidebar-ui.md) | 与 Chat 解耦、两种形态、防空转、层级序号、**Web 怎么确定没问题** |
| [03 回放与 FAQ](03-replay-and-faq.md) | 同一 `applyEvent`、中途停止、原文 FAQ |
