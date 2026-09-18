# 07. 写一个 Agent

底座不包装 `Agent`。你建普通 Agno agent，再配 `AgentRuntime`。卡片 / Todo / 压缩的细节回链，这里只写装配。

```python
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai.like import OpenAILike
from agno_harness import (
    AgentRuntime,
    HideToolFilter,
    SmartCompressionManager,
    make_checkpoint_hook,
    make_thread_title_hook,
)

db = SqliteDb(db_file="sessions.db")
model = OpenAILike(id="qwen-plus", api_key=..., base_url=...)
compression_manager = SmartCompressionManager(
    model=model, trigger_token_limit=50_000, debug_mode=True,
)

agent = Agent(
    name="assistant",
    model=model,
    db=db,
    tools=[...],
    instructions=INSTRUCTIONS,
    add_history_to_context=True,
    num_history_runs=100,  # 见压缩 01；None 会被 Agno 改成 3
    compress_tool_results=True,
    compression_manager=compression_manager,
    markdown=True,
    telemetry=False,
)
runtime = AgentRuntime(agent=agent, db=db, catalog=catalog)
runtime.on_post_run(make_thread_title_hook(runtime))
runtime.on_post_run(make_checkpoint_hook(runtime))
```

指令里的卡片文档用 `catalog.to_prompt()`，不要手写 fence。Skill 规模上去走 [07 Skills](../02-interactions/07-skills-and-jit.md)。

`OpenAILike` + `telemetry=False` 的原因见 [菜谱 02](../00-agent-cookbook/02-web-fastapi-agent.md)。

## Tools / 过滤器 / `ui_block`

工具返回模型能摘要的 JSON。共享状态改 `run_context.session_state`。多步规划挂 `TodoToolkit`（[03 Todo](../02-interactions/03-todo/README.md)）。长文件挂 `StreamingArtifactToolkit`（[08](../02-interactions/08-streaming-artifacts.md)）。进度卡：`ui_block` / `emit_item`。HITL 声明见 [HITL 01](../02-interactions/02-hitl-and-actions/01-protocol.md)。

```python
runtime.register_tool_filter(HideToolFilter({"load_skill", "think"}))
runtime.register_tool_filter(TransformResultFilter("fetch_report", summarize))
```

过滤器按注册顺序。

长文件工具链（细节在 08）：禁止 `write_file`；0→1 走流式卡；局部改 `read_artifact_section` + `patch_artifact`；截断 `append_artifact`。

## Parser

官方 handler 把一种 Agno chunk 变成 AG-UI 帧。Parser 在**它之后**跑，只能**追加**。官方丢掉的字段（如 Qwen `reasoning_content`）走这里。

签名：`(chunk, state) -> Iterable[BaseEvent]`。`state` 是这次 run 的 Agno `StreamState`。

```python
from ag_ui.core import CustomEvent, EventType
from agno.run.agent import RunEvent
from agno_harness import reasoning_content_parser, subagent_steps_parser

def citations_parser(chunk, state):
    cites = getattr(chunk, "citations", None)
    if not cites:
        return
    yield CustomEvent(type=EventType.CUSTOM, name="citations", value=cites)

runtime.register_parser(RunEvent.run_content, citations_parser)
runtime.register_parser(RunEvent.tool_call_started, subagent_steps_parser(["delegate_subagent"]))
```

同一 `RunEvent` 可挂多个，按注册顺序。`reasoning_content_parser` 默认已挂在 `run_content`。

看原始 chunk：`SequencerMode.AUDIT` + `record_chunks=3` + `expose_debug_routes=True`。目录用 `AGNO_HARNESS_TRACE_DIR`。

| 要 emit | 用 |
| --- | --- |
| 产品自定义（引用、进度） | `CUSTOM`，别抢 `ui.*` / `subagent.*` |
| 步骤条 | `STEP_STARTED` / `STEP_FINISHED`（回放不恢复） |
| 思考 | 不要自己拼 `REASONING_*`，走内置 reasoning parser |

yield 仍过 sequencer 和 modules。没被认领的 `CUSTOM` 由 `custom_events` 归档。前端 reducer **不得丢帧**。

不是 parser：整段 run 前后用 hook；工具内出卡用 `ui_block`；要命名空间和收尾用 `BridgeModule`。

## Hooks

pre-run：改 `scope.user_id` / `session_state` / `run_kwargs`，可 yield 事件（在 `RUN_STARTED` 之后）。

```python
def stamp_tenant(scope):
    scope.session_state["tenant"] = scope.user_id
    return
runtime.on_pre_run(stamp_tenant)
```

post-run：看到 completion，事件落在 `RUN_FINISHED` 之前。标题用 `make_thread_title_hook`，走 `agent.model` 短补全，**不要** `agent.arun()`。再起名：`forwardedProps.refreshTitle`。hook 里不要自己写 store。

委托：[交互 06](../02-interactions/06-multi-agent-delegation.md)。测试换 resolver：[07 Skills §6](../02-interactions/07-skills-and-jit.md)。
