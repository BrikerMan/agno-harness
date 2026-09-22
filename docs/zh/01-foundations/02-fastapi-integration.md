# 01-foundations / FastAPI 生产级双模接入 (FastAPI Integration)

在企业级架构中，智能体通常不是孤立运行的微服务，而是作为能力挂载到企业已有的业务网关、鉴权系统与微服务栈中。

`agno-harness` 提供了**双模接入方案**：
1. **主模式（推荐）：`relay.get_router()` / `make_relay_router()` 挂载模式**；
2. **副模式（独立部署）：`RelayServer` 类继承模式**。

---

## 1. 主模式：挂载到已有 FastAPI 应用 (Primary Mount Mode)

这是推荐的企业级落地方式。你的后端服务拥有既有的路由、中间件、监控与 JWT 鉴权体系。

根据你的业务形态，支持两种平滑接入路径：

### 路径 A：纯 Web / Desktop 应用
如果你正在开发桌面端 Sidecar，或 Web Agent API，你可以直接使用 `make_agui_router` 挂载 `AgentRuntime`：

```python
from fastapi import FastAPI
from agno_harness import AgentRuntime, LongRunManager, make_agui_router

app = FastAPI(title="Agent Sidecar")

# 1. 初始化 Runtime 与 LongRunManager
runtime = AgentRuntime(agent=agent, db=db, catalog=catalog)
long_runs = LongRunManager(runtime)

# 2. 直接挂载标准 AG-UI Router
agui_router = make_agui_router(
    runtime,
    long_runs=long_runs,
    resolve_user_id=resolve_local_user,
    expose_debug_routes=True,
    include_health=True,
)
app.include_router(agui_router)
app.include_router(agui_router, prefix="/api")
```

### 路径 B：跨渠道多端部署（Teams、飞书、CLI、Web 统一网关）
当需要将 Agent 一键分发到企业 IM（Teams / 飞书）及 Web 时，使用 `RelayApp` 与 `make_relay_router`（或 `relay.get_router()`）：

```python
from fastapi import FastAPI, Request
from agno.agent import Agent
from agno_harness import AgentRuntime, RelayApp, TeamsChannel, LarkChannel

# 1. 组装核心执行引擎（输出标准 AG-UI 流）
runtime = AgentRuntime(agent=agent)

# 2. 构建 RelayApp 全渠道网关并添加渠道
relay = RelayApp(runtime=runtime)
relay.add_channel(TeamsChannel(bot_app_id="...", bot_app_password="..."))
relay.add_channel(LarkChannel(app_id="...", app_secret="..."))

app = FastAPI(title="Enterprise Bot Gateway", lifespan=relay.lifespan)

# 2. 服务端鉴权函数 (Fail-Fast & Fail-Loud)
def resolve_enterprise_user(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    return verify_jwt_and_get_user(auth)

# 3. 一键打包挂载（同时挂载 Web AG-UI 端点及所有 Channel 的 Webhook 端点）
app.include_router(
    relay.get_router(
        prefix="/agent",
        resolve_user_id=resolve_enterprise_user,
        tags=["AI Agent"],
    )
)
```

### 挂载后自动拥有的能力：
- `GET /agent/health`：就绪探针，`{ status, channels, resumeMode }`（`none` / `history` / `live`）。React kit 第一次发送前读 `resumeMode`。
- `POST /agent/agui`：标准 AG-UI SSE 协议通信管道；
- `GET /agent/threads`：基于当前登录用户隔离的会话列表；
- `GET /agent/threads/{id}/messages`：历史消息重放；
- 如果 `relay.add_channel(teams_channel)`，Teams Webhook 是 `POST /agent/api/messages`。渠道路由固定是 `/api/messages`，router 的 `prefix` 加在前面。没有 `/agent/teams/messages` 这条路由。

---

## 2. 身份解析铁律：Fail-Fast & Fail-Loud

安全第一原则：**绝不信任客户端请求体中的用户 ID**。

### 启动期强约束 (Startup Validation)
如果调用 `relay.get_router()` 或 `make_relay_router()` 时没有提供 `resolve_user_id`，且没有显式指定 `allow_anonymous=True`，系统会在启动时**直接抛出 `ConfigurationError` 崩溃**：

```python
# ❌ 错误示范：未配置身份解析函数，启动即报错
router = relay.get_router()  
# ConfigurationError: resolve_user_id is required in production to authenticate callers...

# ✅ 本地调试示范：显式开启匿名模式
router = relay.get_router(allow_anonymous=True)

# ✅ 生产示范：注入鉴权函数
router = relay.get_router(resolve_user_id=resolve_user)
```

### 运行时强拦截 (Runtime 401 Rejection)
当客户端发起 `POST /agui` 或读取 `GET /threads` 时：
- 如果 `resolve_user_id(request)` 返回 `None`，网关会**立刻返回 `HTTP 401 Unauthorized`**；
- 绝不会降级为匿名执行，杜绝未登录用户读取或写入其他会话的风险。

---

## 3. 副模式：`RelayServer` 开箱即用类模式 (Standalone Mode)

对于希望快速独立启动独立 Agent 微服务的场景，`RelayServer` 提供了面向对象（OOP）继承能力：

```python
from agno_harness.server import RelayServer
from fastapi import Request

class MyEnterpriseServer(RelayServer):
    def resolve_user_id(self, request: Request) -> str | None:
        """重写身份解析逻辑"""
        return request.headers.get("X-Staff-Id")

    def setup_middleware(self) -> None:
        """追加企业审计中间件"""
        super().setup_middleware()
        # self.add_middleware(...)

    def setup_routes(self) -> None:
        """追加自定义路由"""
        super().setup_routes()
        
        @self.get("/api/v1/ping")
        async def ping():
            return {"pong": True}

# 实例化并运行
server = MyEnterpriseServer(relay)
if __name__ == "__main__":
    server.run(host="0.0.0.0", port=8000)
```
