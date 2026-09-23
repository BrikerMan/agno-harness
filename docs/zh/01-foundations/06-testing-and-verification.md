# 01-foundations / 测试策略与质量验证 (Testing & Verification)

`agno-harness` 拥有一套严格的工业级工程测试规范。**严禁在单元测试中发起真实的大模型网络请求**。

---

## 1. 测试原则与工具栈

- **包管理与运行**：采用 `uv`，运行 `make check`（包含 `ruff` 检查与格式化、`mypy` 强类型静态检查、`pytest` 全套用例）；
- **Mock Agent 机制**：Agent 只需满足 `arun(**kwargs)` 返回可异步迭代的 Agno Chunk。使用测试夹具 `FakeAgent` 进行确定性脚本化模拟；
- **分层隔离检测**：`tests/test_layering.py` 静态扫描 AST，确保 `core/`、`runtime/` 不非法反向导入 FastAPI 或外部 SDK。

---

## 2. 常用单测模式

### 2.1 协议时序测试 (Sequencer Modes)
- `SequencerMode.STRICT`：一旦发生协议时序违规（如未发送 `TEXT_MESSAGE_START` 就下发内容）直接报错；
- `SequencerMode.AUDIT`：生产默认模式，自动静默修复协议时序错乱，并记录修复指标。

```python
from agno_harness import AgentRuntime, SequencerMode
from tests.conftest import FakeAgent, content, run_completed, make_input

runtime = AgentRuntime(
    agent=FakeAgent([content("hello"), run_completed()]),
    sequencer_mode=SequencerMode.AUDIT,
)
events = [e async for e in runtime.stream_events(make_input())]
assert [e.type for e in events] == [
    "RUN_STARTED",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "RUN_FINISHED",
]
```

### 2.2 挂载与身份鉴权测试
验证外部 FastAPI 应用挂载与 401 鉴权拦截：
```python
from fastapi import FastAPI
from fastapi.testclient import TestClient
from agno_harness import RelayApp

relay = RelayApp(runtime)
app = FastAPI()
app.include_router(relay.get_router(resolve_user_id=lambda req: req.headers.get("X-User-Id")))

client = TestClient(app)
# 未提供认证头返回 401
assert client.post("/api/v1/channels/web/agui", json={...}).status_code == 401
# 携带合法认证头通过
assert client.post("/api/v1/channels/web/agui", headers={"X-User-Id": "u1"}, json={...}).status_code == 200
```

### 2.3 Golden Trace 替换 resolver

录制 / 回放卡片帧时，生产 resolver 会打真实 DB 或外网，Golden 会抖。测试里整体替换：

```python
catalog.replace_resolvers({
    "movie": lambda data: {"title": "Alien", "rating": 8.4, "year": 1979},
})
```

`data` 仍是模型出的 ID；`resolved` 变成夹具。契约与 JIT 见 [07 Skills](../02-interactions/07-skills-and-jit.md)。

### 2.4 HITL

用 FakeAgent 吐 `run_paused`，再发 **trailing** `role: tool` 恢复。四种 `pauseType` 都要测；resume 必须落下同一 `toolCallId` 的 `TOOL_CALL_RESULT`。答案 JSON 见 [HITL 01](../02-interactions/02-hitl-and-actions/01-protocol.md)。

### 2.5 长任务

`LongRunManager` + `InMemoryRunEventLog`（或 `Stores` 的内存 stand-in）。断言：`POST ?long-run=1` 后掐掉 HTTP，`/active` 仍 running；`/attach?after=` 能续；**paused 的 tail 必须结束**，不能挂死测试。Stop 后有 `CUSTOM run.cancelled`。路由职责见 [持久化 02](05-persistence-and-longruns/02-longrun-routes.md)。

真 LLM 用 pytest marker `integration`，默认不要跑。
