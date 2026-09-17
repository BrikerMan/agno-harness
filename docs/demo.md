# Demo

何时读：要跑 playground，或对照场景。已知行为 bug 只记在 [demo-issues.md](demo-issues.md)，不在这里修。

```bash
make run            # 前端后台 + 后端前台
make run-be         # http://localhost:8020
make run-fe         # http://localhost:5173  （/api → 后端）
```

模型：环境变量 → `examples/demo/backend/.env` → UI Model 面板。用 `OpenAILike`（兼容网关；不要用 `OpenAIChat`）。`telemetry=False`。`REDIS_URL` 默认 `memory://`（单进程 live）；真 Redis 才能跨 worker follow；`none` 关掉 resume。

用户切换器信 `X-Demo-User`，不要抄进生产。

Debug 面板（bug 图标 / `⌘\`）来自 [`examples/frontend-kit/agui-devtools/`](../examples/frontend-kit/agui-devtools/)，demo 从该目录 import。Chunks 需要 `expose_debug_routes=True`（demo 已开）。

场景列表由后端 `/scenarios` 下发。HITL 四场景怎么点、答案长什么样 → [hitl.md](hitl.md)。线程标题 hook 已挂；侧栏「新任务」动画尚未做（demo-issues #2）。

本地 parser 追踪：`make run-be TRACE_DIR=traces` 然后 `make trace`。OTLP 见 [observability.md](observability.md)。
