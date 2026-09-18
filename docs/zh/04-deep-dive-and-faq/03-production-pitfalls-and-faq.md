# 生产落地避坑指南与高频 FAQ 全景 (Production Pitfalls & FAQ)

本文档面向架构师与核心开发人员，汇集了从真实生产环境（包括数十万日活企业智能体实践）中提炼出的核心暗坑、高频 FAQ 以及数据库原生 JSONB 高级检索技巧。

---

## 一、 生产避坑五大核心法则

### 1. 绝不对 IM 平台做逐 Token 打字机流式更新 (Rate-Limit Meltdown)
- **踩坑**：在 Teams 或飞书中尝试模拟网页版打字机，高频调用 `edit_message`，3 秒内必遭平台封锁（`HTTP 429 Too Many Requests`）；
- **对策**：IM 通道一律配置 `stream_mode="final"`（默认），模型思考完整后一次性推送；如必须模拟打字效果，必须使用 `stream_mode="throttle"`，节流窗口严格 `>= 1.5 秒`。

### 2. 鉴权失败必须立即阻断，严禁静默降级为“匿名访客” (Fail-Fast & Fail-Loud)
- **踩坑**：用户鉴权函数返回 `None` 或异常时，网关未拦截，自动当成公用 Guest 用户继续读取，导致员工数据串号与越权；
- **对策**：`agno-harness` 强制开启启动期强校验（缺少 `resolve_user_id` 直接报 `ConfigurationError`），运行时鉴权失败立即抛出 `HTTP 401 Unauthorized`。

### 3. 会话拓扑必须支持业务级解耦 (User-Defined SessionKeyResolver)
- **踩坑**：单聊群聊混用一个 `chat_id`，导致同一个群里不同同事提问时上下文互相污染；
- **对策**：默认拓扑为单聊按会话隔离、群聊与帖子按发信人隔离，同时允许业务传入 `session_resolver` 函数，按工单编号、客户 ID 等自定义拼接。

### 4. 大模型思考耗时必须有 Webhook 幂等防御 (Webhook Retry Storms)
- **踩坑**：大模型 CoT 耗时 5 秒，飞书/Teams 判定超时重试 3 次，导致同一个问题并发运行 3 遍；
- **对策**：`RelayApp` 内置 `enable_deduplication=True`，在滑动时间窗口内自动对 `event_id` 去重并静默阻断重试。

### 5. 平台 Reaction 必须用 `try...finally` 安全闭环 (Reaction Ghost State)
- **踩坑**：收到消息打了“思考中”表情，后续模型报错崩溃，思考中表情永远挂在聊天窗口；
- **对策**：`RelayApp` 统一由底层安全保障：成功打 `✅`，失败捕获异常并安全修正为 `❌`，绝不遗留假死状态。

---

## 二、 原始数据与解析数据双重落库（Dual Storage）与 PostgreSQL JSONB 进阶

在 `agno-harness` 中，消息持久化审计采用 **Dual Storage 原则**：

```sql
CREATE TABLE IF NOT EXISTS agno_message_audits (
    id BIGSERIAL PRIMARY KEY,
    direction VARCHAR(16) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    chat_id VARCHAR(128) NOT NULL,
    thread_id VARCHAR(128),
    sender_id VARCHAR(128),
    session_id VARCHAR(128) NOT NULL,
    text TEXT NOT NULL,             -- 解析清洗后的干净 Markdown（用于语义检索与对话重放）
    raw_text TEXT,                  -- 原始输入富文本/HTML（用于高保真还原）
    raw_payload_json JSONB,         -- 原始平台完整 JSON Payload（用于线上纠纷与 100% 审计定责）
    cards_json JSONB,               -- 渲染的交互式卡片 JSON
    extra_json JSONB,               -- 结构化附件清单、回调动作 ID 与返回值
    created_at TIMESTAMPTZ NOT NULL
);
```

### PostgreSQL 原生 JSONB 查询检索实战
由于使用了方言自适应的 `JSONVariant`，在 PostgreSQL 下字段物理类型为二进制 `JSONB`：

```sql
-- 1. 查询所有带有特定审批动作记录的消息
SELECT id, sender_id, text, extra_json->>'action_id' AS action
FROM agno_message_audits
WHERE extra_json->>'action_id' = 'agno.hitl.resume';

-- 2. 查询所有上传了 PDF 附件的提问
SELECT chat_id, text, extra_json->'attachments'
FROM agno_message_audits
WHERE extra_json @> '{"attachments": [{"content_type": "application/pdf"}]}';

-- 3. 在生产库中建立高性能 GIN 索引
CREATE INDEX idx_audits_extra_gin ON agno_message_audits USING gin (extra_json);
CREATE INDEX idx_audits_raw_gin ON agno_message_audits USING gin (raw_payload_json);
```

---

## 三、 高频 FAQ

### Q1：为什么强烈不推荐全局 `@relay.action` 装饰器，而推荐 Class-First？
**答**：
在 Demo 中写 `@relay.action("btn")` 看起来很简单，但在生产大型项目中，这种写法会带来严重问题：
1. **循环导入地狱**：卡片定义文件为了注册 handler 必须导入 `relay` 实例，而定义 `relay` 的主入口又要导入卡片，跨文件相互依赖极易引发 `ImportError: cannot import name ...`；
2. **高内聚破损**：一个卡片的渲染逻辑、数据解析与点击回调被拆散在不同文件，改动一个字段往往漏改其他地方；
3. **命名空间冲突**：不同开发者在不同模块写了同名 action（如 `"submit"`），全局字典会发生覆盖静默 Bug。

**Class-First 模式**将 Schema、Resolver、Renderer 与 Action Handler 完全封装在同一个自包含类内，彻底杜绝上述缺陷。

### Q2：群聊为什么需要 Chime-in（自主插话）策略？
**答**：
在飞书群或 Teams 团队中，如果开通了机器人的群消息读取权限，群内任何两名员工聊天，机器人都会收到事件。
若无插话过滤，机器人会对每句话发起大模型推理，不仅群聊遭到严重刷屏干扰，Token 费用也将激增几十倍。
`agno-harness` 内置了 `MentionOnlyPolicy`（严格最小权限）：**仅在单聊私信、或群聊中被 `@机器人` 时才触发运行**，其余消息 0 LLM 开销。

### Q3：本地开发没有公网 IP 和备案域名，如何调试飞书和 Teams？
**答**：
- **飞书**：推荐使用 **WebSocket 长连接模式**（`LarkChannel(use_websocket=True)`），直接在本地运行即可与飞书云端实时通信，无需任何公网 IP、端口映射或 Nginx 配置；
- **Teams**：运行 `agno-harness teams onboard`，配合免费的 `devtunnel` 或 `ngrok`（如 `ngrok http 8000`）将本地暴露为临时安全链接即可。
