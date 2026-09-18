# 08. 流式 Artifact

长报告、多页 HTML、整仓源码：**禁止**挂 `write_file(content: str)` / 全量 `edit_file`。几千行 JSON 入参会卡住事件循环，前端假死；漏一个引号整文件报废。

前端限高与内部跟滚：[Web 03](../03-clients/01-web-react/03-stream-and-scroll.md)。

## 1. 工具链矩阵

| 阶段 | 用 | 禁 |
| --- | --- | --- |
| 0 → 1 创建 | `StreamingArtifactToolkit`（`ArtifactCard` / `PresentationDeck`） | `write_file` |
| 审查 | `read_artifact_section` | 全量 `read_file` |
| 1 → N 改局部 | `patch_artifact` | 整份重生成 |
| 截断续写 | `append_artifact` / `mode: "append"` | 从第 1 行重写 |

```python
from agno_harness import (
    AgentRuntime,
    ArtifactCard,
    CardCatalog,
    PresentationDeck,
    StreamingArtifactToolkit,
)

catalog = CardCatalog([ArtifactCard, PresentationDeck])
artifact_toolkit = StreamingArtifactToolkit()
agent = Agent(tools=[artifact_toolkit, ...], instructions=catalog.to_prompt())
runtime = AgentRuntime(
    agent=agent,
    catalog=catalog,
    artifact_root_dir="data/artifacts/{task-id}",  # {thread-id} / {run-id} / {user-id}
)
```

路径拒绝 `../`。`ui.block.end` 注入 `savedPath` / `relativePath` / `bytes`。

## 2. 两种卡片

**透明长文 `ArtifactCard`：** `schema_name = "artifact"`，`emit_text = True`。fence 里写原生 Markdown，不转义 JSON：

```xml
<stream-ui>
{"schema": "artifact", "title": "架构报告", "path": "reports/arch.md"}
# 架构报告

内部可以套 ```python 代码块，不会提前截断。
</stream-ui>
```

工具内：`await emit_artifact(title, content, path=...)`。前端 Header + Meta + Content，默认限高。

**静默大文件 `PresentationDeck`：** `emit_text = False`。`parse_line` 识别 `<!-- SLIDE: X - Title -->`，抽成轻量 `ui.item` 进度，不把几千行 HTML 灌进 SSE。

`BlockSchema` 钩子：`emit_text`、`parse_line`、`on_complete`（闭合时落盘或回写）。

## 3. 三个运维工具

**`read_artifact_section(filepath, start_line=None, line_count=50, query=None)`**  
按行号或关键字切片，带上下文。返回 `start_line` / `end_line` / `total_lines` / `has_more` / `content`。

**`patch_artifact(filepath, search_block, replace_block, title=None)`**  
定位替换；多处匹配报错；成功后 `ui_block("diff")` 出红绿审查卡。返回 `replaced_lines_span` / `lines_added` / `lines_removed`。

**`append_artifact(filepath, content)`**  
尾部追加，检测 HTML 闭合与幻灯片数。返回 `appended_bytes` / `is_complete_html` / `slide_count`。

协议层续写：

```xml
<stream-ui>
{"schema": "presentation_deck", "filepath": "output/deck.html", "mode": "append"}
<!-- SLIDE: 4 -->
</stream-ui>
```

`block.truncated === true` 时前端出未完成条（如 `3 of 5 slides`），引导用户让 Agent 续写。

进度卡也可用 `ui_block` / `emit_item`，不必走 fence。

`read_artifact_section` 返回 `start_line` / `end_line` / `total_lines` / `has_more` / `content`。  
`patch_artifact` 返回 `replaced_lines_span` / `lines_added` / `lines_removed`，并 `ui_block("diff")`。  
`append_artifact` 返回 `appended_bytes` / `is_complete_html` / `slide_count`。

## 4. 前端：Header + Meta + Content

长卡绝不能在聊天流里无限拉长。

- **视口：** 默认 `h-[340px]` + `overflow-y-auto`，底部淡出；按钮切全高。
- **内部跟滚：** 流式时自动向下；用户上翻立刻停（`userScrolledUp`），出「Follow live output」。和主列表的 stick **分开**。
- **Meta：** `ui.block.end` 的 `savedPath` / `bytes` 写在标题下，提供复制 / 下载。
- **截断：** `block.truncated === true` 出警告条（`3 of 5 slides`），不要假装写完。
- **注册：** 按 `schema_name` 绑组件（`artifact`、`presentation_deck`、`diff`）。`diff` 不要进 System Prompt（`include_in_system_prompt = False`）。

主列表跟滚规则仍在 [Web 03](../03-clients/01-web-react/03-stream-and-scroll.md)。

## 5. Web 怎么确定没问题

| # | 操作 | 必须看到 |
| --- | --- | --- |
| 1 | 让 Agent 写一份长 Markdown 报告 | 字在卡片**里面**往下长，聊天气泡高度不跟着暴涨；气泡里没有整份 `write_file` JSON |
| 2 | 卡片内上翻再等几秒 | 内部跟滚停；主列表如果钉在底部不受影响；点「回到底部」才继续跟 |
| 3 | 写到一半刷新 | `/frames` 回放能看到已写出的正文；`truncated` 则有未完成条 |
| 4 | 让 Agent `patch_artifact` 改一段 | 出红绿 Diff 卡；文件 Meta 的 bytes / 行数变化；没有整份重生成 |
| 5 | token 截断后再说「继续」 | `append` 接在原文后面，前面不重写；幻灯片页码接着走 |
| 6 | 路径带 `../` | 服务端拒绝，前端看到错误，磁盘没有逃出 `artifact_root_dir` |

没有挂 `write_file` 才算后端对；上面六条全绿才算 Web 对。
