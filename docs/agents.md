# 写 Agent

何时读：接一个 Agno agent。委托见 [multi-agent.md](multi-agent.md)，卡片见 [cards.md](cards.md)。

toolbox **不包装** `Agent`。你建普通 Agno agent，再配 runtime。

```python
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai.like import OpenAILike

from agno_relay import (
    AguiRuntime,
    HideToolFilter,
    SmartCompressionManager,
    make_checkpoint_hook,
    make_thread_title_hook,
)

db = SqliteDb(db_file="sessions.db")
model = OpenAILike(id="...", api_key=..., base_url=...)

compression_manager = SmartCompressionManager(
    model=model,
    trigger_token_limit=12000,
    debug_mode=True,
)

agent = Agent(
    name="assistant",
    model=model,
    db=db,
    tools=[...],
    instructions=INSTRUCTIONS,
    add_history_to_context=True,
    num_history_runs=100,  # 务必设为 100（由 SmartCompressionManager 托管记忆，详见 docs/compression.md；设为 None 会被 Agno 默认成 3 导致硬截断失忆）
    compress_tool_results=True,
    compression_manager=compression_manager,
    markdown=True,
    telemetry=False,
)
runtime = AguiRuntime(agent=agent, db=db, catalog=catalog)
runtime.on_post_run(make_thread_title_hook(runtime))
runtime.on_post_run(make_checkpoint_hook(runtime))
```

指令里卡片文档用 `catalog.to_prompt()` 生成，不要手写 fence。

## Tools / filters

工具返回模型能摘要的 JSON。共享状态改 `run_context.session_state`。多步骤任务规划直接挂载 `TodoToolkit`（详见 [todo-workflow.md](todo-workflow.md)）。长报告与大文件生成挂载 `StreamingArtifactToolkit`（详见 [cards.md](cards.md)）。进度卡用 `ui_block` / `emit_item`。HITL 用 Agno 的 `requires_confirmation` / `requires_user_input` / `external_execution` / `user_feedback_schema`。答案 JSON → [hitl.md](hitl.md)。

```python
from agno_relay import StreamingArtifactToolkit, TodoToolkit

# 1. 标准 Todo 工具（默认单层扁平清单；若需要子步骤传 allow_subtasks=True）
todo_tool = TodoToolkit(default_title="任务规划", allow_subtasks=False)

# 2. 长文档与大文件生成工具（自动引导模型走流式卡片，避免传统 write_file / edit_file 阻塞卡死）
# 同时提供 read_artifact_section, patch_artifact, append_artifact 三大运维工具
artifact_toolkit = StreamingArtifactToolkit()

agent = Agent(
    tools=[todo_tool, artifact_toolkit, ...],
    instructions="...",
)
```

### 长文件与复杂工件工具链规范 (Long Files Toolchain)

当应用需要生成长文档（Markdown 报告、方案书）或代码/演示文件（多页 HTML 幻灯片、完整项目源码）时，请遵循以下规范：

1. **禁止挂载全量写文件工具**：
   - 严禁向此类 Agent 注入传统 `write_file(content: str)` 或全量 `edit_file`。几千行长文本的 JSON 入参不仅会阻塞 Agno 事件循环导致前端数分钟无输出（假死感），且只要结尾漏了一个引号就会全部损毁。
2. **0 -> 1 初始生成**：
   - 统一走 `<stream-ui>` 卡片流式输出（`ArtifactCard` 透明文本流、`PresentationDeck` 静默生成 + 进度抽取）。
3. **定向探查与精准局部修改 (1 -> N)**：
   - 当用户要求修改某几行或某页时，Agent 调用 `read_artifact_section` 检索相关窗口（避免将 3000 行 HTML 灌满上下文引发遗忘与幻觉）。
   - Agent 调用 `patch_artifact` 提供 `search_block` 和 `replace_block`。工具在后台安全替换，**自动在 UI 流中渲染高亮 Diff 审查卡**，并返回结构化富元数据（包括变更行号区间、增删行数）。
4. **断点续写与恢复**：
   - 当遇到输出截断时，Agent 调用 `append_artifact` 接续追加剩余内容，或以 `<stream-ui> {"schema": "...", "mode": "append"} ...` 续写，避免重复生成前序内容。

```python
runtime.register_tool_filter(HideToolFilter({"load_skill", "think"}))
runtime.register_tool_filter(TransformResultFilter("fetch_report", summarize))
runtime.register_tool_filter(HideToolFilter({toolkit.tool_names}))  # 委托工具本身
```

过滤器按注册顺序。

### 技能与卡片动态加载 (`SkillManager`)

面对大量业务技能与卡片（如 20 个 Skill、每个 5 张卡片），通过 `SkillManager` 进行目录扫描或字符串注入，并与 `CardCatalog` 联动实现启动期 fail-fast 校验与运行时 JIT 动态注入：

```python
from agno_relay import CardCatalog, HideToolFilter, SkillManager

# 1. 注册全量卡片
catalog = CardCatalog([...])

# 2. 启动时扫描或注入技能（strict=True 严格校验卡片存在性与 Frontmatter 语法）
skill_manager = SkillManager(catalog=catalog, sources=["skills/"], strict=True)

# 3. 创建挂载给 Agent 的 load_skill 工具
load_skill_tool = skill_manager.create_load_skill_tool()

agent = Agent(
    tools=[load_skill_tool, ...],
    instructions=f"{BASE_PROMPT}\n{catalog.to_prompt()}\n{skill_manager.to_roster_prompt()}",
)

# 4. 隐藏 load_skill 工具帧，仅在命中时静默向上下文注入该技能的 instructions 与卡片 schema
runtime = AguiRuntime(agent=agent, catalog=catalog)
runtime.register_tool_filter(HideToolFilter({"load_skill"}))
```
详见 [cards.md](cards.md)（含 §5 存量系统迁移与 Prompt 清洗清单）。

## Parser：从 Agno chunk 多 emit 事件

官方 handler 把一种 Agno chunk 变成 AG-UI 帧。Parser 在**它之后**跑，只能**追加**，不能替换、不能吞掉官方输出。官方丢掉的字段（如 Qwen 的 `reasoning_content`）或协议里没有的概念，走这里。

签名：`(chunk, state) -> Iterable[BaseEvent]`。同步 generator，`yield` 即上线。`state` 是这次 run 的 Agno `StreamState`，可以往上挂自己的属性。

```python
from ag_ui.core import CustomEvent, EventType
from agno.run.agent import RunEvent

from agno_relay import reasoning_content_parser, subagent_steps_parser


def citations_parser(chunk, state):
    cites = getattr(chunk, "citations", None)
    if not cites:
        return
    yield CustomEvent(type=EventType.CUSTOM, name="citations", value=cites)


runtime.register_parser(RunEvent.run_content, citations_parser)
# 也接受字符串：runtime.register_parser("RunContent", citations_parser)
```

同一 `RunEvent` 可以挂多个 parser，按注册顺序。`reasoning_content_parser` 默认已经挂在 `run_content` 上。委托步骤标记：

```python
runtime.register_parser(RunEvent.tool_call_started, subagent_steps_parser(["delegate_subagent"]))
runtime.register_parser(RunEvent.tool_call_completed, subagent_steps_parser(["delegate_subagent"]))
```

写之前先看原始 chunk 长什么样：`SequencerMode.AUDIT` + `record_chunks=3` + `expose_debug_routes=True`，Chunks 页是转换**前**的 Agno 形状。`BETTER_AGNO_TRACE_DIR` 出 chunk → 事件对照。

| 要 emit 什么 | 用 |
| --- | --- |
| 产品自定义（引用、来源、进度文案） | `CUSTOM`，名字别和模块命名空间抢（`ui.*` / `subagent.*`） |
| 步骤条 | `STEP_STARTED` / `STEP_FINISHED`（回放故意不恢复） |
| 思考 | 不要自己拼 `REASONING_*`，喂给 Agno 的 `on_reasoning_content_delta`（见内置 parser） |

yield 出去仍过 sequencer 和 modules。没被模块认领的 `CUSTOM`，`custom_events` 会归档，刷新还在。前端 reducer **不得丢帧**，未知 `CUSTOM` 才能画出来。

不是 parser 的场景：整段 run 前后用 hook；工具内部出卡片用 `ui_block` / `emit_item`；要命名空间和收尾关括号用 `BridgeModule`。

## Hooks

pre-run：改 `scope.user_id` / `session_state` / `run_kwargs`，可 yield 事件（在 `RUN_STARTED` 之后）。post-run：看到 completion，事件落在 `RUN_FINISHED` 之前。`CUSTOM` 没被模块认领的，由 `custom_events` 归档——hook 里不要自己写 store。

线程标题用 `make_thread_title_hook`，走 `agent.model` 短补全，**不要** `agent.arun()`。再起名：`forwardedProps.refreshTitle`。

## HITL

流正常结束、等人。恢复时 messages **末尾** `role: tool`。四种暂停与答案 JSON → [hitl.md](hitl.md)。`user_feedback` 挂 Agno 的 `UserFeedbackTools`，模型调 `ask_user`（不是 `@tool` 装饰器）。
