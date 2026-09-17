# 上手

何时读：从零搭一个会流式输出的 AG-UI 服务。前端完整壳见 [frontend-ui.md](frontend-ui.md)。

## 安装

```bash
uv add "agno-relay[fastapi,sqlite]"
```

Python 3.11+。模型走 Agno。

## OpenAILike 铁律

文档和新建服务默认：

```python
from agno.models.openai.like import OpenAILike
from agno.agent import Agent

agent = Agent(
    model=OpenAILike(id="qwen-plus", api_key=..., base_url="https://..."),
    telemetry=False,
)
```

`OpenAIChat` 走官方 OpenAI（`developer` role 等）。兼容网关（OpenRouter / DashScope / vLLM）必须 `OpenAILike`，否则 400 或 thinking 对不上。

`telemetry=False` 必须留：Agno 会在 run 结束后等一段遥测 POST，SSE 会假死在收尾。Demo 也用 `OpenAILike`。

## 最小服务

```python
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai.like import OpenAILike
from fastapi import FastAPI

from agno_relay import AguiRuntime, make_agui_router

db = SqliteDb(db_file="sessions.db")
agent = Agent(model=OpenAILike(id="gpt-4o-mini", api_key=...), db=db, telemetry=False)
runtime = AguiRuntime(agent=agent, db=db)

app = FastAPI()
app.include_router(make_agui_router(runtime))
```

```bash
uv run uvicorn app:app --reload
```

`POST /agui`，body 是 `RunAgentInput`：

```bash
curl -N localhost:8000/agui -H 'content-type: application/json' -d '{
  "threadId": "t1", "runId": "r1",
  "messages": [{"id": "m1", "role": "user", "content": "hello"}],
  "state": {}, "context": [], "tools": [], "forwardedProps": {}
}'
```

### 路由

| Route | 作用 |
| --- | --- |
| `POST /agui` | 跑 agent，SSE。`?debug=1` 追加 `debug.summary`；`?detach=1` 需要 `long_runs` |
| `GET /threads` | 当前用户的 threads |
| `GET /threads/{id}/messages` | 从 Agno session 重建（lossy，给模型用） |
| `GET /threads/{id}/frames` | 当时流过的帧。没有 durable log 则 501 |
| `DELETE /threads/{id}` | 删 thread |

挂上 `long_runs` 才有 `GET /runs/{id}/attach`（旧 `/stream` 已 deprecated）、`POST /runs/{id}/abort`、`GET /threads/{id}/active`。没挂就是路由 404。

## 谁在说话（`resolve_user_id`）

每条对话（thread / run）都记在某个用户名下。路由器靠你传入的函数认出「这是谁」：

```python
from fastapi import Request


def resolve_user_id(request: Request) -> str | None:
    # 生产：读你自己的登录中间件已经验证过的身份
    return request.state.user.id


app.include_router(make_agui_router(runtime, resolve_user_id=resolve_user_id))
```

不传这个函数 = 单用户模式，所有人共用同一份 thread 列表。本地玩具可以；上线必须传。

**从哪读：** 只从请求里服务端已经信任的地方读——登录 cookie、session、鉴权中间件写到 `request.state` 的用户。不要从 JSON body 里读 `userId`：那是调用者自己填的，谁都能写成别人。

Demo 用 `X-Demo-User` header 假装登录，方便切换 alice/bob。生产不要抄这个 header，换成真正的登录即可，函数签名不用改。

**找不到或不是你的：** 一律 `404`，不要 `403`。`threadId` 是前端生成的，别人猜得到。`403` 等于告诉攻击者「这个 id 存在，只是不属于你」；`404` 和「根本没有」长得一样。

前端换用户时必须 `reset` 屏幕上的 transcript，不能把上一个人的气泡留着。

## 无 FastAPI

```python
async for event in runtime.stream_events(run_input, user_id="alice"):
    ...
```

`transport/` 以下不 import FastAPI。

## 然后补齐

1. `resolve_user_id` — 否则所有 thread 属于所有人。见上一节。
2. `Stores` + history archive +（resume 用）Redis 热 log。见 [persistence.md](persistence.md)。
3. `LongRunManager` + lifespan 里 `await long_runs.shutdown()`。
4. 卡片：[cards.md](cards.md)。
5. 线程标题：`runtime.on_post_run(make_thread_title_hook(runtime))`。没有新 HTTP 路由。
6. Debug：`expose_debug_routes=True` 才有 `/debug/chunks`。前端复制 [`examples/frontend-kit/agui-devtools/`](../examples/frontend-kit/agui-devtools/)。

开发时：`SequencerMode.AUDIT` + `record_chunks=3` + `BETTER_AGNO_TRACE_DIR=traces`。
