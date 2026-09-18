# 02-interactions / Class-First 自包含卡片体系 (Class-First Card Components)

在多渠道企业级 Agent 应用中，卡片组件是承载富交互的核心媒介。传统基于全局装饰器（`@catalog.renderer`）注册的做法在大型项目中容易引发**跨文件循环导入**与**全局状态污染**。

`agno-harness` 采用 **Class-First** 自包含模式：**一个卡片组件就是一个自包含的 Python 类**。它将结构约束（Schema Contract）、服务端权威事实注入（Server-Side Fact Resolution）以及多端呈现（Teams / 飞书 / CLI 渲染器）聚合在同一处。

规模上去之后的 Skill JIT、`data` vs `resolved`：[07 Skills](07-skills-and-jit.md)。长文档不要 `write_file`：[08 Artifact](08-streaming-artifacts.md)。

---

## 1. 核心设计原则

1. **紧凑契约 (Minimal Schema)**：大模型不应幻觉输出只读事实（如海报 URL、当前价格、评级）。模型只需填写关键主键（ID）与操作附言；
2. **事实注入 (Fact Resolution)**：卡片类的异步 `resolve(self, ctx)` 方法从数据库或权威 API 拉取只读事实，保证呈现真实准确；
3. **平台渲染多态 (Multi-Target Rendering)**：一个类中直接实现各平台渲染方法：
   - `render_teams(self, resolved)`：返回 Teams Adaptive Cards 片段；
   - `render_lark(self, resolved)`：返回飞书 Interactive Cards v2 片段；
   - `render_cli(self, resolved)`：返回 Rich 控制台渲染文本；
4. **自适应降级 (Graceful Degradation)**：若某个平台未显式实现渲染逻辑，`CardCatalog` 自动生成 FactSet 或 Markdown 兜底，决不崩溃报错。

---

## 2. 完整实战范例：`MovieCard`

```python
from typing import Any
from agno_harness.core.streamui.schema import ItemSchema

class MovieCard(ItemSchema):
    """电影推荐富卡片组件"""
    
    schema_name = "movie"
    
    # 1. 结构契约：让大模型填写的字段尽量轻量
    movie_id: int
    recommend_reason: str = ""

    # 2. 服务端事实注入：根据 movie_id 异步查库或调 API 补充权威事实
    async def resolve(self, ctx: Any = None) -> dict[str, Any]:
        # 模拟从数据库或第三方电影库拉取权威数据
        # 权威数据永远不要让 LLM 凭空幻觉捏造！
        return {
            "title": f"Interstellar #{self.movie_id}",
            "rating": "8.7",
            "year": 2014,
            "poster_url": "https://example.com/poster.jpg",
        }

    # 3. Teams 端自适应渲染 (Adaptive Card JSON Fragment)
    def render_teams(self, resolved: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "Container",
            "separator": True,
            "items": [
                {
                    "type": "TextBlock",
                    "text": resolved.get("title", f"Movie #{self.movie_id}"),
                    "weight": "Bolder",
                    "size": "Medium",
                },
                {
                    "type": "TextBlock",
                    "text": f"★ {resolved.get('rating')} · {resolved.get('year')}",
                    "spacing": "None",
                },
                {
                    "type": "TextBlock",
                    "text": self.recommend_reason,
                    "isSubtle": True,
                    "wrap": True,
                },
            ],
        }

    # 4. 飞书端自适应渲染 (Lark Interactive Card v2 Fragment)
    def render_lark(self, resolved: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{resolved.get('title')}** (★ {resolved.get('rating')})\n_{self.recommend_reason}_",
            },
        }

    # 5. CLI 终端 Rich 渲染 (ANSI Console)
    def render_cli(self, resolved: dict[str, Any]) -> str:
        return f"[bold cyan]{resolved.get('title')}[/] (★ {resolved.get('rating')})\n  [dim]{self.recommend_reason}[/]"
```

---

## 3. 批量聚合：N 合 1 卡片呈现 (N-Items to 1 Card)

当 Agent 向用户推荐 10 部电影时，如果直接向 Teams 或飞书发送 10 条独立的消息或卡片：
- 会严重刷屏，打乱聊天界面；
- 极易瞬间触发企业 IM 平台的 Webhook 发送频率限制（429 Too Many Requests）。

`agno-harness` 支持通过 `BlockSchema` 将 N 个卡片片段整合成一张自适应复合卡片：

```python
from agno_harness.core.streamui.schema import BlockSchema

class MovieListBlock(BlockSchema):
    schema_name = "movie-list"
    body = "items"
    default_item = "movie"

    def render_teams(self, props: dict[str, Any], rendered_items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "body": [
                {
                    "type": "TextBlock",
                    "text": props.get("title", "🎬 推荐电影清单"),
                    "weight": "Bolder",
                    "size": "Large",
                },
                *rendered_items,
            ],
        }
```

注册给网关使用：

```python
from agno_harness.core.streamui.schema import CardCatalog

catalog = CardCatalog([MovieListBlock, MovieCard])
relay = RelayApp(agent, card_catalog=catalog)
```

---

## 4. Class-First 天然命名空间：彻底告别动作命名冲突

在大型系统中，多个卡片常常包含同名的交互动作（例如“提交表单”、“立即重试”、“点赞”、“取消”）。全局扁平的注册方式极易导致命名冲突与覆盖。

在 Class-First 模式下，**每个卡片类的 `schema_name` 就是天然的命名空间（Namespace）**，开发者再也无需为全局名字唯一性操心：

```python
class FeedbackCard(BlockSchema):
    schema_name = "feedback"

    @classmethod
    async def handle_action(cls, action: str, payload: dict[str, Any], event: Any) -> Any:
        if action == "submit":
            # 这里的 "submit" 归属于 feedback 空间，与其它卡片的 "submit" 互不干扰！
            await db.save_score(payload.get("score"))
            return None # 静默盖戳

class OrderCard(BlockSchema):
    schema_name = "order"

    @classmethod
    async def handle_action(cls, action: str, payload: dict[str, Any], event: Any) -> Any:
        if action == "submit":
            # 这里的 "submit" 归属于 order 空间
            await db.place_order(payload.get("order_id"))
            return "订单已提交！"
```

### 两种零心智负担的路由方式：
1. **冒号前缀命名空间 (`schema:verb`)**：
   卡片按钮的 `action_id` 写作 `"feedback:submit"` 或 `"order:submit"`，`CardCatalog.dispatch_action` 会自动剥离前缀并精准路由到对应卡片类的 `handle_action("submit", ...)`。
2. **Payload 显式声明 (`schema`)**：
   卡片按钮的 `data` / `value` 中携带 `{"schema": "feedback", "action": "submit"}`，网关直接定向派发。

