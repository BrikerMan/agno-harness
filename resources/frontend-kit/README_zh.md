# frontend-kit

把本目录复制进产品。**不是 npm 包。**

这是 `agno-harness` 的 AG-UI 协议层：一个 `applyEvent` 同时吃 live SSE、`/frames` 回放、`/attach` 续写。对应文档 [Web / React 五步](../../docs/zh/03-clients/01-web-react/README.md) 的可运行实现。

**不是** Chat UI。侧栏、Composer、气泡、卡片渲染器仍由产品自己写。

## 复制

```text
frontend-kit/
  agui.ts                 类型 + body.order[]
  apply-event.ts          纯 reducer（live / frames / attach）
  sse.ts                  fetch SSE、id: cursor、: ping idle
  use-agui-chat.ts        发送 / Stop / attach / HITL 续上
  use-stick-to-bottom.ts  发送时钉住；只有用户上翻才脱离
  index.ts
```

Peer：React 18+。没有其它运行时依赖。

```ts
import { useAguiChat } from "./frontend-kit";

const chat = useAguiChat({
  apiBase: "/api",                 // Vite 把 /api 剥掉，指到后端根路径
  storageKey: "my-app:session",
  getHeaders: () => ({ "X-User-Id": userId }),
  debug: import.meta.env.DEV,
});
```

Vite：

```ts
proxy: {
  "/api": {
    target: "http://localhost:8000",
    changeOrigin: true,
    rewrite: (p) => p.replace(/^\/api/, ""),
  },
}
```

## 服务端合同

线上帧是 AG-UI `EventEncoder`：`{ "type": "RUN_STARTED", ... }`。
不是 `{ "event": "run_started" }`。

```python
from agno_harness import AgentRuntime, make_agui_router
from agno_harness.runtime.longrun import LongRunManager

app.include_router(
    make_agui_router(
        runtime,
        long_runs=LongRunManager(runtime, log=hot, stream=hot),
        resolve_user_id=resolve_user,
        include_health=True,          # GET /health → { resumeMode }
        expose_debug_routes=True,     # 可选 DevTools chunks
    ),
    prefix="/api",                    # 或代理 /api → "" 且不设 prefix
)
```

`make_relay_router` / `relay.get_router()` 已经挂 `GET /health`，带 `resumeMode`。

hook 必须在第一次发送前读到 `resumeMode`。缺字段会被当成 `"none"`，刷新永远不会 attach。

| 客户端 | 服务端 |
| --- | --- |
| `GET {apiBase}/health` | `{ status, resumeMode: "none" \| "history" \| "live" }` |
| `POST {apiBase}/agui?long-run=1` | SSE。头 `X-Agui-Resume`、`X-Agui-Protocol: 1.0` |
| `GET {apiBase}/runs/{id}/attach?after=` | 从 SSE `id:` 游标续 |
| `POST {apiBase}/runs/{id}/abort` | 只对应 Stop，不是关 tab |
| `GET {apiBase}/threads/{id}/frames` | 同一套 `applyEvent` 回放 |
| `GET {apiBase}/threads/{id}/active` | running / paused |

身份走服务端信任的 header（`resolve_user_id`）。不要把 `userId` 放进 `RunAgentInput`。

## 不要

- 用 `GET /threads/{id}/messages` 重画气泡
- 把 tool 从 `body.order[]` 捞出来另画
- 关 tab 就 `POST .../abort`
- 把 attach 的 `id:` 和 frames 的 `{runId}:{offset}` 混用
- 把 `: ping` 当 AG-UI 事件，或 idle 时 abort 任务

产品壳（标题、四态侧栏、HITL 表单、卡片）仍走五步。本目录只负责事件流。
