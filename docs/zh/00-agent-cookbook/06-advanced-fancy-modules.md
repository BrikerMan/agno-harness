# 06. 进阶与 Fancy 扩展模块全景（Advanced & Fancy Modules）

除了标准的单聊与多渠道会话，`agno-harness` 还内置了一批专为企业级高阶场景打造的“Fancy”扩展模块。

本章为你盘点这些核心进阶武器，包括 **发给模型的用户请求（时间戳）**、**文档附件 OCR 解析**、**自主插话（Chime-in）策略**、**全链路彩色可观测日志** 与 **PostgreSQL 原生 JSONB 审计链**。

发给 LLM 的用户请求为什么要拼时间，见 [User Query Envelope](../02-interactions/05-user-query.md)。始终开启，一般不用再配。

---

## 1. 可插拔附件处理器（AttachmentProcessor）

在企业办公中，员工频繁在聊天窗口发送 PDF 合同、报表 Excel 或原型图片。

`agno-harness` 将平台各异的文件拉取与下载彻底抽象为 `InboundAttachment`，并通过 `AttachmentProcessor` 协议提供可插拔接口：

```python
from agno_harness import AttachmentProcessor, ChannelEvent, InboundAttachment

class EnterpriseDocMindProcessor(AttachmentProcessor):
    """企业自定义附件处理器：业务自由对接内部 OCR、阿里云 DocMind 或 AWS Textract"""

    async def process(
        self,
        attachments: list[InboundAttachment],
        event: ChannelEvent,
    ) -> str | None:
        extracted_texts = []
        for att in attachments:
            # 根据 platform 和 att.id 下载文件并调用 OCR
            # 业务拥有绝对控制权，例如校验租户权限、文件白名单
            extracted_texts.append(f"【文件: {att.name} 内容】:\n本合同约定交货周期为30个工作日...")
        
        # 返回提取文本，agno-harness 会自动将其注入到 Agent 运行上下文（RunAgentInput.context）
        return "\n\n".join(extracted_texts) if extracted_texts else None

# 挂载到 RelayApp
relay = RelayApp(
    runtime=runtime,
    attachment_processor=EnterpriseDocMindProcessor(),
)
```

---

## 2. 自主插话与最小权限策略（ChimeInPolicy）

在百人群或千人大群中，如果机器人开通了群消息读取权限，默认每句话都会收到事件。若不加节制地调用大模型，不仅会引发严重的群聊刷屏干扰，Token 成本更会暴增数十倍。

`agno-harness` 提供了开箱即用的 **自主插话策略引擎**：

```python
from agno_harness import MentionOnlyPolicy, KeywordChimeInPolicy

# 策略 1：严格最小权限（仅在单聊私聊、或群聊中被 @ 时才响应）
policy = MentionOnlyPolicy(bot_names=["智能助理", "Assistant"])

# 策略 2：基于关键字自主插话（如群里出现“求助”、“报障”、“bug”且经过评估时才插话）
keyword_policy = KeywordChimeInPolicy(keywords=["故障", "报警", "谁能帮我看下"])

relay = RelayApp(
    runtime=runtime,
    chime_in_policy=policy, # 未命中策略的消息直接 0 LLM 消耗静默丢弃
)
```

---

## 3. 全链路专业彩色日志服务（Relay Logging）

系统调试最怕无上下文的杂乱输出。`agno-harness` 提供了带渠道徽章（Badges）与会话链路追踪的彩色日志服务：

```python
from agno_harness import setup_relay_logging, get_relay_logger

# 1. 终端开发模式：输出带彩色 Badge 的可读日志
setup_relay_logging(level="DEBUG", format_type="console")

# 2. 生产 Kubernetes / 容器模式：一行切为结构化 JSON 日志
# setup_relay_logging(level="INFO", format_type="json")

log = get_relay_logger("custom_service")
log.info("Processing inbound event", extra={"chat_id": "c-101", "platform": "lark"})
```

### 控制台输出视觉效果：
```text
12:00:01 INFO  [LARK]  Received message event: '分析合同' (chat_id=oc_123, sender_id=ou_456)
12:00:02 INFO  [AGNO]  Starting LLM turn with gpt-4o (thread_id=lark:oc_123:ou_456)
12:00:04 INFO  [TEAMS] Dispatched Adaptive Card to user (action_id=order.approve)
```

---

## 4. 原始与解析数据双重落库（Dual Storage & PostgreSQL JSONB）

在涉及金钱审批、生产变更或客户纠纷的企业系统中，一旦出现问题，**必须拥有 100% 完整的原始报文证据链**。

`agno-harness` 采用严格的 **双重落库原则（Dual Storage）**：
- `text`: 清洗解析后的干净 Markdown（用于对话历史重放与向量检索）；
- `raw_text`: 各端原始输入的 HTML 或未清洗文本（用于高保真复原）；
- `raw_payload_json`: 平台原始推送的完整 JSON 报文（用于线上问题定责复盘）；
- `extra_json`: 包含附件元数据、动作 ID 及参数。

在 PostgreSQL 环境中，底层自动启用 **原生二进制 `JSONB`**：

```sql
-- 查询所有包含特定采购审批动作的历史日志
SELECT id, sender_id, text, extra_json->>'action_id' AS action
FROM agno_message_audits
WHERE extra_json->>'action_id' = 'approve_deploy';

-- 查询所有上传过 PDF 文件的员工咨询记录
SELECT chat_id, text, extra_json->'attachments'
FROM agno_message_audits
WHERE extra_json @> '{"attachments": [{"content_type": "application/pdf"}]}';

-- 生产高性能 GIN 索引
CREATE INDEX idx_audits_extra_gin ON agno_message_audits USING gin (extra_json);
CREATE INDEX idx_audits_raw_gin ON agno_message_audits USING gin (raw_payload_json);
```
