# 07. Skills 与 JIT 卡片注入

Class-First 契约仍看 [01](01-class-first-cards.md)。本篇只写规模上去之后：20 个 Skill、每个 5 张卡，**不能**把 100 张 XML 塞进初始 System Prompt。

那会浪费 10k–15k Token，稀释注意力，闲聊时误画冷门卡，也分不清「工具自己 emit」和「模型手搓 fence」。

## 1. `include_in_system_prompt`

`diff`、`todo-list`、搜索结果卡由工具 `ui_block` / `emit_item` 发射，模型不该在正文里写 XML：

```python
class DiffCard(BlockSchema):
    schema_name = "diff"
    body = "text"
    include_in_system_prompt: ClassVar[bool] = False
```

`CardCatalog.to_prompt()` 默认跳过它们。把「不要手写某某卡片」的负向提示词删掉——模型看不见语法，就不会幻觉。

## 2. Frontmatter 与多对多

每个技能 Markdown 的 YAML 声明所需卡片：

```markdown
---
name: financial_audit
description: 审计财报，生成高管摘要与指标卡片
cards:
  - stat-row
  - table
  - artifact
---
# Financial Audit Guidelines
核心数据走 stat-row；明细走 table；备忘录走 artifact。
```

`stat-row` 可被多个 Skill 引用。正文里不要再粘一整段 `<stream-ui>` 样例——和 Python schema 会漂。

## 3. `SkillManager` 启动校验

支持目录扫描（`skills/*/SKILL.md`）、单文件、内存字符串：

```python
from agno_harness import CardCatalog, HideToolFilter, SkillManager

catalog = CardCatalog([...])  # 全量注册，后端校验用
skill_manager = SkillManager(
    catalog=catalog,
    sources=["skills/", raw_markdown_from_db],
    strict=True,
)
```

- `strict=True`（默认）：YAML、`name` / `description`、重名、**卡片是否在 catalog 里**。缺卡抛 `SkillValidationError`，启动失败。
- `strict=False`：`logger.warning` 并跳过。

## 4. JIT：`load_skill` + roster

启动时 System Prompt 只放基础卡 + 约 200 token 的技能清单：

```python
load_skill_tool = skill_manager.create_load_skill_tool()
agent = Agent(
    tools=[load_skill_tool, ...],
    instructions=f"{BASE_PROMPT}\n{catalog.to_prompt()}\n{skill_manager.to_roster_prompt()}",
)
runtime = AgentRuntime(agent=agent, catalog=catalog)
runtime.register_tool_filter(HideToolFilter({"load_skill"}))
```

命中意图时模型调 `load_skill("financial_audit")`，工具返回该技能 instructions + `catalog.to_prompt(include=skill.cards)`。

不要传 Agno `Agent(skills=...)`——它会注入一长段无关工具规则。用 `to_roster_prompt()`。

主 `instructions` 里的手写 XML 也删掉，交给 `catalog.to_prompt()`。

| | 全量塞进 Prompt | SkillManager + JIT |
| --- | --- | --- |
| 初始 System | 10k–15k | 约 400–600 |
| 闲聊画错卡 | 高 | 未 load 前模型不知道这张卡 |
| 负向提示词 | 一堆 Don't | 零 |
| 缺卡发现 | 运行时前端报错 | 启动 Fail-Fast |

## 5. 前端 `data` vs `resolved`

模型只出 ID + note；服务端 `resolve` 补事实。事件上：

- `value.data` — LLM 原始字段
- `value.resolved` — 服务端补全
- `value.resolveError` — 超时 / API 挂了；卡片不要消失

```tsx
const title = String(resolved.title ?? `#${String(data.id ?? "?")}`);
```

`resolved` 空时回退 ID 与 note，并画降级提示。媒体域名走 catalog 白名单。

## 6. Golden Trace 换 resolver

生产 resolver 打真实 DB / 外网。测试与 Golden 回放要确定性：

```python
catalog.replace_resolvers({
    "movie": lambda data: {"title": "Alien", "rating": 8.4, "year": 1979},
})
```

验收：[06 测试](../01-foundations/06-testing-and-verification.md)。
