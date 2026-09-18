# 07. Writing an agent

The base does not wrap `Agent`. You build a normal Agno agent, then wire `AgentRuntime`. Card / todo / compression details live on their own pages; this is assembly only.

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
    num_history_runs=100,  # see compression 01; None becomes 3 inside Agno
    compress_tool_results=True,
    compression_manager=compression_manager,
    markdown=True,
    telemetry=False,
)
runtime = AgentRuntime(agent=agent, db=db, catalog=catalog)
runtime.on_post_run(make_thread_title_hook(runtime))
runtime.on_post_run(make_checkpoint_hook(runtime))
```

Card docs in the instructions come from `catalog.to_prompt()`, not a hand-written fence. At skill scale use [07 Skills](../02-interactions/07-skills-and-jit.md).

Why `OpenAILike` + `telemetry=False`: [cookbook 02](../00-agent-cookbook/02-web-fastapi-agent.md).

## Tools / filters / `ui_block`

Tools return JSON the model can summarize. Shared state goes on `run_context.session_state`. Multi-step plans use `TodoToolkit` ([03 Todos](../02-interactions/03-todo/README.md)). Long files use `StreamingArtifactToolkit` ([08](../02-interactions/08-streaming-artifacts.md)). Progress cards: `ui_block` / `emit_item`. HITL declarations: [HITL 01](../02-interactions/02-hitl-and-actions/01-protocol.md).

```python
runtime.register_tool_filter(HideToolFilter({"load_skill", "think"}))
runtime.register_tool_filter(TransformResultFilter("fetch_report", summarize))
```

Filters run in registration order.

Long-file toolchain (detail on 08): no `write_file`; 0→1 is a streaming card; local edits are `read_artifact_section` + `patch_artifact`; a cut uses `append_artifact`.

## Parser

The official handler turns one Agno chunk into AG-UI frames. A parser runs **after** it and may only **append**. Fields the official path drops (Qwen `reasoning_content`) go here.

Signature: `(chunk, state) -> Iterable[BaseEvent]`. `state` is this run’s Agno `StreamState`.

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

Several parsers may hang on one `RunEvent`, in order. `reasoning_content_parser` is already on `run_content`.

Inspect raw chunks with `SequencerMode.AUDIT` + `record_chunks=3` + `expose_debug_routes=True`. The dump directory is `AGNO_HARNESS_TRACE_DIR`.

| You want to emit | Use |
| --- | --- |
| Product extras (citations, progress copy) | `CUSTOM`, do not collide with `ui.*` / `subagent.*` |
| Step bar | `STEP_STARTED` / `STEP_FINISHED` (not restored on replay) |
| Thinking | Do not invent `REASONING_*`; use the built-in reasoning parser |

Yields still pass the sequencer and modules. Unclaimed `CUSTOM` is archived by `custom_events`. The frontend reducer **must not drop frames**.

Not a parser: hooks around the whole run; cards from inside a tool via `ui_block`; namespaced brackets via `BridgeModule`.

## Hooks

pre-run: mutate `scope.user_id` / `session_state` / `run_kwargs`; you may yield events (after `RUN_STARTED`).

```python
def stamp_tenant(scope):
    scope.session_state["tenant"] = scope.user_id
    return
runtime.on_pre_run(stamp_tenant)
```

post-run: you see the completion; events land before `RUN_FINISHED`. Titles use `make_thread_title_hook` — a short completion on `agent.model`, **not** `agent.arun()`. Rename later with `forwardedProps.refreshTitle`. Do not write the store yourself from a hook.

Delegation: [interaction 06](../02-interactions/06-multi-agent-delegation.md). Swap resolvers in tests: [07 Skills §6](../02-interactions/07-skills-and-jit.md).
