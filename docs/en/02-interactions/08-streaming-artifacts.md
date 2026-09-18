# 08. Streaming artifacts

Long reports, multi-page HTML, a whole tree of source: **do not** mount `write_file(content: str)` or a full-file `edit_file`. Thousands of JSON argument tokens stall the event loop; one missing quote kills the file.

Card height and inner follow: [Web 03](../03-clients/01-web-react/03-stream-and-scroll.md).

## 1. Toolchain matrix

| Stage | Use | Ban |
| --- | --- | --- |
| 0 → 1 create | `StreamingArtifactToolkit` (`ArtifactCard` / `PresentationDeck`) | `write_file` |
| Review | `read_artifact_section` | Full `read_file` |
| 1 → N local edit | `patch_artifact` | Regenerate the whole file |
| Resume a cut | `append_artifact` / `mode: "append"` | Rewrite from line 1 |

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

Paths reject `../`. `ui.block.end` injects `savedPath` / `relativePath` / `bytes`.

## 2. Two cards

**Transparent long text `ArtifactCard`:** `schema_name = "artifact"`, `emit_text = True`. Native Markdown inside the fence, no JSON escaping:

```xml
<stream-ui>
{"schema": "artifact", "title": "Architecture report", "path": "reports/arch.md"}
# Architecture report

Nested ```python fences will not close the card early.
</stream-ui>
```

From a tool: `await emit_artifact(title, content, path=...)`. Frontend: Header + Meta + Content, default max height.

**Silent large file `PresentationDeck`:** `emit_text = False`. `parse_line` picks `<!-- SLIDE: X - Title -->` into light `ui.item` progress. Thousands of HTML lines stay off the SSE.

`BlockSchema` hooks: `emit_text`, `parse_line`, `on_complete` (persist on close).

## 3. Three maintenance tools

**`read_artifact_section(filepath, start_line=None, line_count=50, query=None)`**  
Slice by line or keyword, with context. Returns `start_line` / `end_line` / `total_lines` / `has_more` / `content`.

**`patch_artifact(filepath, search_block, replace_block, title=None)`**  
Locate and replace; error on multiple hits; emit `ui_block("diff")` for a review card. Returns `replaced_lines_span` / `lines_added` / `lines_removed`.

**`append_artifact(filepath, content)`**  
Append at the end; detect HTML close and slide count. Returns `appended_bytes` / `is_complete_html` / `slide_count`.

Protocol-level resume:

```xml
<stream-ui>
{"schema": "presentation_deck", "filepath": "output/deck.html", "mode": "append"}
<!-- SLIDE: 4 -->
</stream-ui>
```

When `block.truncated === true`, show an unfinished bar (e.g. `3 of 5 slides`) and let the user ask the agent to continue.

Progress cards can also use `ui_block` / `emit_item` without a fence.

`read_artifact_section` returns `start_line` / `end_line` / `total_lines` / `has_more` / `content`.  
`patch_artifact` returns `replaced_lines_span` / `lines_added` / `lines_removed` and emits `ui_block("diff")`.  
`append_artifact` returns `appended_bytes` / `is_complete_html` / `slide_count`.

## 4. Frontend: Header + Meta + Content

A long card must not grow the chat stream without a cap.

- **Viewport:** default `h-[340px]` + `overflow-y-auto`, fade at the bottom; a button toggles full height.
- **Inner follow:** scroll down while streaming; stop on user scroll-up (`userScrolledUp`) and show “Follow live output”. **Separate** from the main-list stick.
- **Meta:** `savedPath` / `bytes` from `ui.block.end` sit under the title, with copy / download.
- **Truncated:** `block.truncated === true` shows a warning (`3 of 5 slides`). Do not pretend it finished.
- **Register** by `schema_name` (`artifact`, `presentation_deck`, `diff`). Keep `diff` out of the system prompt (`include_in_system_prompt = False`).

Main-list stick rules stay on [Web 03](../03-clients/01-web-react/03-stream-and-scroll.md).

## 5. How you know the Web is right

| # | Do this | You must see |
| --- | --- | --- |
| 1 | Ask for a long Markdown report | Text grows **inside** the card; the bubble height does not explode; no raw `write_file` JSON in chat |
| 2 | Scroll up inside the card and wait | Inner follow stops; a pinned main list is unaffected; “back to bottom” resumes follow |
| 3 | Refresh mid-write | `/frames` replay shows the text so far; `truncated` shows the unfinished bar |
| 4 | Ask for `patch_artifact` on one section | A red/green diff card; Meta bytes / lines change; the whole file is not regenerated |
| 5 | After a token cut, say “continue” | `append` continues the file; the prefix is not rewritten; slide numbers continue |
| 6 | A path with `../` | The server rejects it; the UI shows an error; nothing lands outside `artifact_root_dir` |

The backend is right only if `write_file` is not mounted. The Web is right only when all six rows are green.
