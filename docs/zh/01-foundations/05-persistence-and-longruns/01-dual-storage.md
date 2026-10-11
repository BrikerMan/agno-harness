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
agno_harness_db = AgnoHarnessSqliteDb(db_file="data/agent.db", prefix="admin_agent")
```

PostgreSQL 生产环境：
```python
from agno.db.async_postgres import AsyncPostgresDb
from agno_harness.db import AgnoHarnessPostgresDb

# 独立 URL 模式
agno_harness_db = AgnoHarnessPostgresDb(db_url=settings.database_url, prefix="admin_agent")

# 或复用已有的 SQLAlchemy async_session_factory（如现有 FastAPI/Django 服务）
agno_harness_db = AgnoHarnessPostgresDb.from_session_factory(
    db.async_session_factory,
    prefix="admin_agent",
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
3. 启动检查或补齐 8 张核心表。

---

## Agno v3 存储：run 独立成表

从 Agno v3 开始，session 行不再携带历史。每个 run 是 `<prefix>_runs`（默认 `agno_runs`）里的一行；读取 session 时，`session.runs` 由这张表重建。

对 harness 的影响：

- `db.upsert_session(session)` **不再写 run**。改了 `run.metadata` 或 `run.messages` 再 upsert session，改动会丢。必须用 `db.upsert_run(run, session_id=..., user_id=...)`。
- `seal_session_run` 和 `attach_checkpoint_to_session` 已经这样做（同步和异步 db 都支持）。尚未适配 v3 的后端，其 `upsert_run` 会抛 `NotImplementedError`，harness 此时回退到 `upsert_session`。
- 如果你直接查 sessions 表的旧 `runs` 列（看板、导出），改查 `<prefix>_runs`（`run_id`、`session_id`、`run_index`、`run_data`）。
- 取消和出错的 run 是 Agno 在自己的清理路径里写入的。封口和它赛跑时，会先短暂等待（最多约半秒）该 run 落库，等不到才回退到最新一个 run。

## 升级 Agno v2 数据库

Agno v3 自带非破坏性迁移：把旧 `runs` JSON 列里的每个 run 复制到 `<prefix>_runs`，补上 `user_id` 列，旧列原样保留当作备份。重复执行不会产生重复 run。

1. 先做可恢复的备份，并停掉所有写入方（应用、worker、调度器）。
2. 执行迁移：

   ```bash
   agno-harness db migrate-agno --sqlite data/agent.db
   agno-harness db migrate-agno --url postgresql+psycopg://user:pass@host/db --prefix admin_agent
   ```

   或者在代码里调用（同步和异步 Agno db 都行）：

   ```python
   from agno_harness import migrate_agno_db

   report = await migrate_agno_db(agno_db)
   print(report.version_before, "->", report.version_after, report.sample_run_ids)
   ```

3. 把旧历史和 `<prefix>_runs` 做**完整**比对，包括抽样之外的 session。抽样无法证明完整。
4. 完成比对并确认备份可恢复之后，才考虑 Agno 的 `cleanup_legacy_runs_column`（SQL）或 `cleanup_legacy_runs_field`（文档 / KV 存储）。agno-harness 从不调用 cleanup，也从不传 `force=True`。

`AgnoHarnessDb.from_agno_db(...)` 在启动时发现配对的 Agno 数据库仍是 v2 结构，会打一条警告。

`--prefix` 必须和应用使用的前缀一致（`AGNO_HARNESS_TABLE_PREFIX` 或你传入的 `prefix=`），因为前缀也会重命名 Agno 自己的表。如果应用给 `SqliteDb` / `PostgresDb` 传了自定义 `session_table=`，用 `--session-table` 传同一个名字；否则命令会去看 `agno_sessions`，找不到表，就报告无需迁移。向量库需要单独迁移，见 [Agno v3 迁移指南](https://docs.agno.com/other/v3-migration)。

---

## 两种表初始化模式

### 模式 A：默认快速启动（非 Alembic 静默建表）

适用于本地开发、原型验证和测试：
- `auto_create=True`（默认值）。
- 启动时自动检查缺失的 harness 表，并直接补齐。
- 输出警告日志提醒生产推荐规范迁移：
  > `[agno-harness] ⚠️ Initialized harness tables automatically for prefix 'admin_agent'. For production environments, it is recommended to manage schema versions via AlembicMigrator.declare_models(Base).`
- 若数据库中已存在 `alembic_version` 表，`auto_create` 会自动跳过并引导使用 Alembic，避免本地产生空 diff。

### 模式 B：企业级 Alembic 规范迁移

生产项目或已有 Alembic 的团队，在模型定义处挂载：

```python
# app/models/__init__.py（Alembic env.py 已经 import 的位置）
from agno_harness.db import AlembicMigrator
from app.models.base import Base

# 单个 Agent
AlembicMigrator.declare_models(base=Base, prefix="admin_agent")

# 多 Agent 共享数据库时注册多次
AlembicMigrator.declare_models(base=Base, prefix="user_agent")
```

随后执行标准迁移流程：
```bash
alembic revision --autogenerate -m "add harness tables"
alembic upgrade head
```

此时生产环境可设置 `auto_create=False`：若表缺失，启动时会抛出带完整修复指引的 `MissingHarnessTablesError`，禁止静默变更数据库。

---

## 前缀与多 Agent 隔离

前缀支持字母、数字、下划线 `_` 和短横线 `-`（如 `admin_agent` 或 `admin-agent`）。
如果前缀包含 `_`，生成的表名采用下划线风格（如 `admin_agent_conversation_sessions`），完全符合 PostgreSQL 规范与现有 DBA 习惯。

8 张表完整列表：
1. `threads`（会话列表与生命周期状态第一等公民）
2. `conversation_sessions`
3. `actions`
4. `message_audits`
5. `custom_events`
6. `run_frames`
7. `run_records`
8. `run_archives`

---

## 依赖原则：数据库必须有，Redis 可用内存替代

在 `agno-harness` 中，外部持久化与流转依赖的定位非常清晰：

1. **关系型数据库（SQLite / PostgreSQL）是必须的，不支持无数据库运行**：
   - 所有的业务实体、UI 会话（`threads`）、会话生命周期、审计日志与归档 frames 均持久化于数据库。
   - `GET /api/v1/threads` 强依赖数据库中的 `threads` 表毫秒级返回，不再从 Agno 庞大的 session runs 中慢速解析反序列化。
2. **Redis 是可选的，支持内存（In-Memory）替代**：
   - Redis 仅用于实时 token 级热事件流（`event_log` / `event_stream`）、阻塞式 Tail 与断点续传（`attach`）。
   - 在单进程、本地开发或轻量级部署中，完全可以使用 `InMemoryRunEventLog` 作为进程内替代方案，无需强制启动独立 Redis 实例。

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
