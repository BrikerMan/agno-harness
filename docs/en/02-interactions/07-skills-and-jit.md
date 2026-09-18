# 07. Skills and JIT card injection

The Class-First contract is still [01](01-class-first-cards.md). This page is what happens at scale: 20 skills, 5 cards each. **Do not** dump 100 XML schemas into the initial system prompt.

That wastes 10k–15k tokens, dilutes attention, paints obscure cards during small talk, and blurs “the tool emitted this” vs “the model wrote a fence”.

## 1. `include_in_system_prompt`

`diff`, `todo-list`, and search-result cards are emitted by tools via `ui_block` / `emit_item`. The model must not write that XML in prose:

```python
class DiffCard(BlockSchema):
    schema_name = "diff"
    body = "text"
    include_in_system_prompt: ClassVar[bool] = False
```

`CardCatalog.to_prompt()` skips them by default. Delete the “do not hand-write card X” negative prompts — if the model never sees the grammar, it cannot hallucinate it.

## 2. Frontmatter and many-to-many

Each skill Markdown declares the cards it needs:

```markdown
---
name: financial_audit
description: Audit financials; emit exec summary and metric cards
cards:
  - stat-row
  - table
  - artifact
---
# Financial Audit Guidelines
Core numbers on stat-row; line items on table; memo on artifact.
```

`stat-row` can be shared. Do not paste a full `<stream-ui>` sample in the body — it will drift from the Python schema.

## 3. `SkillManager` fail-fast

Directory scan (`skills/*/SKILL.md`), a single file, or an in-memory string:

```python
from agno_harness import CardCatalog, HideToolFilter, SkillManager

catalog = CardCatalog([...])  # full register, for server validation
skill_manager = SkillManager(
    catalog=catalog,
    sources=["skills/", raw_markdown_from_db],
    strict=True,
)
```

- `strict=True` (default): YAML, `name` / `description`, duplicates, **card exists in the catalog**. Missing card → `SkillValidationError`, process dies.
- `strict=False`: `logger.warning` and skip.

## 4. JIT: `load_skill` + roster

At boot the system prompt holds base cards plus a ~200-token roster:

```python
load_skill_tool = skill_manager.create_load_skill_tool()
agent = Agent(
    tools=[load_skill_tool, ...],
    instructions=f"{BASE_PROMPT}\n{catalog.to_prompt()}\n{skill_manager.to_roster_prompt()}",
)
runtime = AgentRuntime(agent=agent, catalog=catalog)
runtime.register_tool_filter(HideToolFilter({"load_skill"}))
```

On intent the model calls `load_skill("financial_audit")`. The tool returns that skill’s instructions plus `catalog.to_prompt(include=skill.cards)`.

Do not pass Agno `Agent(skills=...)` — it injects a long block of unrelated tool rules. Use `to_roster_prompt()`.

Delete hand-written XML from the main `instructions`; `catalog.to_prompt()` owns it.

| | Dump everything | SkillManager + JIT |
| --- | --- | --- |
| Initial system | 10k–15k | ~400–600 |
| Wrong card in small talk | High | The model does not know the card until load |
| Negative prompts | A pile of Don’t | None |
| Missing card | Frontend error at runtime | Fail-fast at boot |

## 5. Frontend `data` vs `resolved`

The model emits an id + note; the server `resolve` fills facts. On the event:

- `value.data` — raw LLM fields
- `value.resolved` — server enrichment
- `value.resolveError` — timeout / API down; the card must not vanish

```tsx
const title = String(resolved.title ?? `#${String(data.id ?? "?")}`);
```

If `resolved` is empty, fall back to id and note and show a degraded hint. Media hosts go through the catalog allow-list.

## 6. Swap resolvers on a Golden Trace

Production resolvers hit a real DB / network. Tests and Golden replay must be deterministic:

```python
catalog.replace_resolvers({
    "movie": lambda data: {"title": "Alien", "rating": 8.4, "year": 1979},
})
```

Verification: [06 Testing](../01-foundations/06-testing-and-verification.md).
