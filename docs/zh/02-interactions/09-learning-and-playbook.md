# 09. 持续学习与 Living Playbook

用户教给 Agent 的规则、纠正、术语和群聊偏好，应该随着对话越攒越准，而不是会话一关就忘，或者把 10 份静态 Playbook 重复塞进历史消息。

`agno_harness.learning` 只操作**本地目录 `root_dir: Path`**（零云存储 SDK 耦合、零数据库表迁移）；外部对象存储同步（如 COS 镜像 / `rclone`）和特定渠道的群消息拉取通过极简钩子和纯文本入口由上层应用完成。

## 1. 三层本地目录结构

```text
data/learning/
├── README.md                  # 自动声明 owner_agent，标注其他 Agent 的只读与禁写边界
├── playbook.md                # Layer 1：全局核心规范（每轮热注入 messages[0]）
├── mistakes.md                # Layer 1：纠错本（时间倒序置顶，每轮热注入）
├── CHANGELOG.md               # 全局可读变更摘要（时间倒序）
├── topics/                    # Layer 2：分主题词表/知识页（自动生成目录索引，正文按需 read/grep）
│   ├── README.md
│   └── <slug>.md
├── scopes/                    # Layer 3：个人与群聊交互偏好（按 RunContext 自动隔离注入）
│   ├── README.md
│   ├── users/<slug>-<hash>.md
│   └── groups/<slug>-<hash>.md
└── audit/                     # 独立文件原子快照、回滚点与巡检水位游标
    ├── README.md
    ├── changes/<ts>-<id>.json
    └── cursors/<key>.json
```

## 2. 每轮动态热生效（零历史重复）

不要在进程启动时把 `playbook.md` 拼成静态字符串，也不要每轮放进 `<user-query>`。改用 `LearningStore.compose_instructions(...)` 生成 Agno 动态 callable `instructions(run_context=...)`，每轮在 `messages[0]` 现场渲染最新三层内容与当前群/个人偏好：

```python
from agno.agent import Agent
from agno_harness import LearningStore, LearningToolkit

learning = LearningStore("data/learning", seed_dir="app/learning", owner_agent="coordinator")

agent = Agent(
    name="Coordinator",
    model=model(),
    instructions=learning.compose_instructions(BASE_INSTRUCTIONS),
    tools=[LearningToolkit(learning)],
)
```

`AgentRunner` 会自动把 `platform`、`chat_id`、`sender_id`、`sender_name`、`is_direct_message` 透传进 `RunContext.metadata`。工具层的 `read` / `grep` / `list_files` / `write_file` / `patch_file` 会严格隐藏并拦截 `scopes/` 与 `audit/`，防止跨群与跨人偏好泄漏。

## 3. 审计回滚与防写坏自愈

- **每次修改都有独立不可变快照**：`audit/changes/<ts>-<id>.json` 保存 `before_content`、`after_content`、`after_sha256`、`diff`、操作人及原因，多进程写不会互相覆盖。
- **Tombstone 防复活**：`revert(change_id)` 回滚新建文件或清空偏好时，写入 `status: archived` 墓碑头而不是仅做物理删除，防止外部双向 `rclone copy --update` 把旧文件拷回来。
- **读时免崩与 `doctor()` 自愈**：如果文件被外部进程截断、写入乱码或破坏 frontmatter，`read_text()` 自动回退到最近一条合法快照；调用 `learning.doctor(repair=True)` 或工具 `repair_learning()` 可批量修补缺失 `README.md`、修复损坏文件并捕获/回滚外部 SHA-256 漂移。

## 4. 上层同步钩子与会话/群聊后台提炼

- **本地文件同步钩子**：若上层需要镜像到远程存储，传入 `on_before_read(rel_path)` 和 `on_after_write(rel_path)` 即可，底座保持纯本地。
- **会话关闭与群聊自动提炼（`LearningDigestor`）**：
  - `attach_session_close_digest(relay.session_manager, LearningDigestor(learning, model()), sink)` 会在会话关闭或跨天重置时异步复盘增量对话。
  - 上层定时任务拉到群聊新消息后，直接调用 `await digestor.digest_transcript(text, source_key=..., cursor=..., chat_id=...)` 即可，底座自动通过 `audit/cursors/` 游标去重，无干货默认返回 `NOOP`。
