# 01. 双存储

完整历史是 **Agno `db` + harness `Stores`**，谁也替不了谁。

- **Agno `db`**（`Agent` 和 `AgentRuntime` 上的 `SqliteDb` / `PostgresDb`）— 下一轮模型消息。缺了它，frames 在盘上也没用，模型每轮从零开始。
- **Harness `Stores`** — 用户看过的流。缺了它，`/api/v1/threads/{id}/frames` 还原不了当时的卡片 / 工具 / 思考。

Harness 已是持久 SQL、Agno `db` 却缺失或内存时，`AgentRuntime` 抛 `StoragePairingError`，除非 `allow_ephemeral_agno_db=True`。

Harness 内部还有一层拆分：

- **热 log**（`RedisRunEventLog`，或进程内 stand-in）— 每个 AG-UI delta、可阻塞 tail、TTL。`?long-run=1` 和 `GET /api/v1/runs/{id}/attach` 读它。不要用 SQL 装 token。
- **History archive**（SQL）— 结束后合成的事件列表。连续 content delta 折成一帧。`GET /api/v1/threads/{id}/frames` 走这里。

---

## 快速接入手册：双 DB 架构

在工程中，Agno 原生数据库与 Harness 数据库分工明确：

### 1. 数据库定义 (`base.py`)

```python
from agno.db.sqlite import SqliteDb
from agno_harness.db import AgnoHarnessSqliteDb

# 1. Agno 原生 DB（模型上下文记忆）
agno_db = SqliteDb(db_file="data/agent.db")

# 2. Harness DB（UI 事件流、交互卡片、消息审计、会话管理）
agno_harness_db = AgnoHarnessSqliteDb(db_file="data/agent.db", prefix="ipv")
```

PostgreSQL 生产环境：
```python
from agno.db.async_postgres import AsyncPostgresDb
from agno_harness.db import AgnoHarnessPostgresDb

# 独立 URL 模式
agno_harness_db = AgnoHarnessPostgresDb(db_url=settings.database_url, prefix="ipv")

# 或复用已有的 SQLAlchemy async_session_factory（如现有 FastAPI/Django 服务）
agno_harness_db = AgnoHarnessPostgresDb.from_session_factory(
    db.async_session_factory,
    prefix="ipv",
)
```

### 2. Agent 绑定 (`agent.py`)

Agent 只关注它自己的 Agno 数据库：
```python
the_agent = Agent(
    name="agent",
    description="agent",
    db=agno_db,
)
```

### 3. Runtime 接入 (`main.py` / `runtime_factory.py`)

```python
runtime = AgentRuntime(
    agent=the_agent,
    harness_db=agno_harness_db,
    catalog=CARD_CATALOG,
)
```

`AgentRuntime` 会自动：
1. 将 `harness_db.prefix` 对齐到 `agent.db`。
2. 自动构建并绑定持久化 `stores`。
3. 启动检查或补齐 7 张核心表。

---

## 两种表初始化模式

### 模式 A：默认快速启动（非 Alembic 静默建表）

适用于本地开发、原型验证和测试：
- `auto_create=True`（默认值）。
- 启动时自动检查缺失的 harness 表，并直接补齐。
- 输出警告日志提醒生产推荐规范迁移：
  > `[agno-harness] ⚠️ Initialized harness tables automatically for prefix 'ipv'. For production environments, it is recommended to manage schema versions via AlembicMigrator.declare_models(Base).`
- 若数据库中已存在 `alembic_version` 表，`auto_create` 会自动跳过并引导使用 Alembic，避免本地产生空 diff。

### 模式 B：企业级 Alembic 规范迁移

生产项目或已有 Alembic 的团队，在模型定义处挂载：

```python
# app/models/__init__.py（Alembic env.py 已经 import 的位置）
from agno_harness.db import AlembicMigrator
from app.models.base import Base

# 单个 Agent
AlembicMigrator.declare_models(base=Base, prefix="ipv")

# 多 Agent 共享数据库时注册多次
AlembicMigrator.declare_models(base=Base, prefix="admin")
```

随后执行标准迁移流程：
```bash
alembic revision --autogenerate -m "add harness tables"
alembic upgrade head
```

此时生产环境可设置 `auto_create=False`：若表缺失，启动时会抛出带完整修复指引的 `MissingHarnessTablesError`，禁止静默变更数据库。

---

## 前缀与多 Agent 隔离

前缀支持字母、数字、下划线 `_` 和短横线 `-`（如 `ipv` 或 `ipv_agent`）。
如果前缀包含 `_`，生成的表名采用下划线风格（如 `ipv_conversation_sessions`），完全符合 PostgreSQL 规范与现有 DBA 习惯。

7 张表完整列表：
1. `conversation_sessions`
2. `actions`
3. `message_audits`
4. `custom_events`
5. `run_frames`
6. `run_records`
7. `run_archives`

---

## 流与断点续跑 (`X-Agui-Resume`)

`Stores` 上 `event_log` 与 `event_stream` 分开：可以只有 SQL log（history），再加上 Redis 才是 live。`resume_mode` 由此算出。

| 配了什么 | `X-Agui-Resume` | 行为 |
| --- | --- | --- |
| 没有 | `none` | 连接掉 = run 停 |
| 只有 `event_log` | `history` | 后台跑完；不能句中 follow |
| 再加上 `event_stream`（Redis 或同实现） | `live` | `after=` 接到句中 |

跨进程 live 需要真 Redis。`memory://` 只服务本进程。写 log 失败标 `unrecordable`，不杀流。

两种 cursor 禁止混用：SSE `id:` = 单 run log offset（给 **attach**）；frames id = `{runId}:{paddedOffset}`（只给 `/frames`）。见 [Web 04](../../03-clients/01-web-react/04-attach-and-longrun.md)。

下一步：[02 长任务路由](02-longrun-routes.md)。
