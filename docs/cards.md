# Class-First 卡片组件指南 (Card Engine)

在 `agno-relay` 中，我们坚持 **“模型选主键，服务端补事实”（The model chooses, server supplies the facts）**，并采用 **Class-First** 自包含组件设计，杜绝跨文件全局装饰器注册造成的循环导入。

---

## 1. 为什么弃用装饰器注册？

传统装饰器写法在多卡片、跨文件项目中存在致命问题：
```python
# 传统写法容易导致循环导入死锁与初始化顺序 bug
@catalog.resolver("movie")
async def resolve_movie(...): ...

@catalog.renderer("teams", "movie")
def render_teams(...): ...
```

在 `agno-relay` 中，一个卡片组件就是一个自包含的 Python 类：
- 数据结构由 Pydantic 字段定义（自动生成 Prompt 与校验）；
- 异步事实由 `resolve()` 方法补齐；
- 平台专属 UI 由 `render_teams()`, `render_lark()`, `render_cli()` 直接提供；
- 哪怕未定义渲染器，Catalog 会**自动降级生成高保真兜底 UI**。

---

## 2. 编写你的第一个卡片组件

### 2.1 列表项卡片（ItemSchema）

```python
from typing import Any
from agno_relay import ItemSchema, BlockSchema, CardCatalog

class MovieItem(ItemSchema):
    """电影推荐项"""
    schema_name = "movie"
    id: int
    note: str | None = None

    async def resolve(self, ctx: Any = None) -> dict[str, Any]:
        """服务端异步查询真实数据，模型无需背诵"""
        # 模拟从数据库或第三方 API 读取真实数据
        return {
            "title": f"Interstellar #{self.id}",
            "rating": "★ 9.3",
            "year": 2014,
        }

    def render_teams(self, resolved: dict[str, Any]) -> dict[str, Any]:
        """Teams Adaptive Card 碎片"""
        return {
            "type": "Container",
            "separator": True,
            "items": [
                {"type": "TextBlock", "text": resolved.get("title", ""), "weight": "Bolder"},
                {"type": "TextBlock", "text": f"{resolved.get('rating')} · {resolved.get('year')}", "spacing": "None"},
                {"type": "TextBlock", "text": self.note or "", "isSubtle": True, "wrap": True},
            ],
        }

    def render_lark(self, resolved: dict[str, Any]) -> dict[str, Any]:
        """飞书 Interactive Card v2 碎片"""
        return {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{resolved.get('title')}** ({resolved.get('year')})\n{resolved.get('rating')}\n_{self.note}_",
            },
        }

    def render_cli(self, resolved: dict[str, Any]) -> str:
        """CLI 终端纯文本/Rich 渲染"""
        return f"🎬 {resolved.get('title')} [{resolved.get('rating')}] - {self.note or ''}"
```

### 2.2 容器块（BlockSchema）与自动聚合

当 Agent 在一次生成中推荐多部电影时，LLM 只需输出：
````text
```stream-ui {"schema": "movie-list", "title": "科幻电影精选"}
{"id": 101, "note": "必看神作"}
{"id": 102, "note": "视听震撼"}
```
````

容器类定义：
```python
class MovieListBlock(BlockSchema):
    schema_name = "movie-list"
    body = "items"
    item = MovieItem
    title: str | None = None

    def render_teams_block(self, rendered_items: list[dict[str, Any]]) -> dict[str, Any]:
        """将所有碎片一次性打包为单张 Teams Adaptive Card"""
        return {
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {"type": "TextBlock", "text": self.title or "推荐列表", "weight": "Bolder", "size": "Large"},
                *rendered_items,
            ],
        }

    def render_lark_block(self, rendered_items: list[dict[str, Any]]) -> dict[str, Any]:
        """将所有碎片一次性打包为单张飞书交互式卡片"""
        return {
            "schema": "2.0",
            "header": {"title": {"content": self.title or "推荐列表", "tag": "plain_text"}},
            "body": {"elements": rendered_items},
        }
```

---

## 3. 注册到 Catalog

只需注册顶层的 Block，它引用的 Item 会自动被关联注册：

```python
catalog = CardCatalog([MovieListBlock])

# 自动生成注入给 Agent 的系统 Prompt
prompt_instructions = catalog.to_prompt()
```
