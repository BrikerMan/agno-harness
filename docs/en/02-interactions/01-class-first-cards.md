# 02-interactions / Class-First self-contained cards (Class-First Card Components)

In a multi-channel enterprise agent, cards are the main surface for rich interaction. A global decorator registry (`@catalog.renderer`) is a common source of **circular imports across files** and **global state pollution** in a large project.

`agno-harness` uses a **Class-First** self-contained model: **one card is one self-contained Python class**. Schema contract, server-side fact resolution, and multi-target rendering (Teams / Lark / CLI) live on that class.

Skill JIT and `data` vs `resolved` at scale: [07 Skills](07-skills-and-jit.md). Do not `write_file` long documents: [08 Artifacts](08-streaming-artifacts.md).

---

## 1. Design principles

1. **Minimal Schema**: the model must not invent read-only facts (poster URL, live price, rating). It fills a primary key (ID) and a short note.
2. **Fact Resolution**: the card's async `resolve(self, ctx)` method loads read-only facts from a database or authoritative API, so the render is accurate.
3. **Multi-Target Rendering**: the same class implements each platform renderer:
   - `render_teams(self, resolved)`: Teams Adaptive Card fragment;
   - `render_lark(self, resolved)`: Lark Interactive Card v2 fragment;
   - `render_cli(self, resolved)`: Rich console text.
4. **Graceful Degradation**: if a platform renderer is missing, `CardCatalog` falls back to a FactSet or Markdown. It does not crash.

---

## 2. Full example: `MovieCard`

```python
from typing import Any
from agno_harness.core.streamui.schema import ItemSchema

class MovieCard(ItemSchema):
    """Movie recommendation card."""

    schema_name = "movie"

    # 1. Minimal contract: the model only fills keys and a short note
    movie_id: int
    recommend_reason: str = ""

    # 2. Server-side facts: load from a DB or API by movie_id
    async def resolve(self, ctx: Any = None) -> dict[str, Any]:
        # Authoritative data must not come from the model
        return {
            "title": f"Interstellar #{self.movie_id}",
            "rating": "8.7",
            "year": 2014,
            "poster_url": "https://example.com/poster.jpg",
        }

    # 3. Teams Adaptive Card fragment
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

    # 4. Lark Interactive Card v2 fragment
    def render_lark(self, resolved: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{resolved.get('title')}** (★ {resolved.get('rating')})\n_{self.recommend_reason}_",
            },
        }

    # 5. CLI Rich text
    def render_cli(self, resolved: dict[str, Any]) -> str:
        return f"[bold cyan]{resolved.get('title')}[/] (★ {resolved.get('rating')})\n  [dim]{self.recommend_reason}[/]"
```

---

## 3. Batch aggregation: N items to 1 card

When the agent recommends 10 movies, sending 10 separate Teams or Lark messages:

- floods the chat and breaks the thread;
- can trip the enterprise IM webhook rate limit (429 Too Many Requests) in one burst.

`agno-harness` can fold N card fragments into one Adaptive composite card with `BlockSchema`:

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
                    "text": props.get("title", "🎬 Recommended movies"),
                    "weight": "Bolder",
                    "size": "Large",
                },
                *rendered_items,
            ],
        }
```

Register the catalog on the gateway:

```python
from agno_harness.core.streamui.schema import CardCatalog

catalog = CardCatalog([MovieListBlock, MovieCard])
relay = RelayApp(agent, card_catalog=catalog)
```

---

## 4. Class-First namespaces: no more action-name collisions

In a large system, many cards share the same verbs ("submit form", "retry", "like", "cancel"). A flat global registry collides and overwrites.

Under Class-First, **each card class's `schema_name` is a natural namespace**. You do not have to keep action names globally unique:

```python
class FeedbackCard(BlockSchema):
    schema_name = "feedback"

    @classmethod
    async def handle_action(cls, action: str, payload: dict[str, Any], event: Any) -> Any:
        if action == "submit":
            # This "submit" lives in the feedback namespace
            await db.save_score(payload.get("score"))
            return None  # silent reaction, no chat bubble

class OrderCard(BlockSchema):
    schema_name = "order"

    @classmethod
    async def handle_action(cls, action: str, payload: dict[str, Any], event: Any) -> Any:
        if action == "submit":
            # This "submit" lives in the order namespace
            await db.place_order(payload.get("order_id"))
            return "Order placed."
```

### Two zero-overhead routing styles:

1. **Colon-prefixed namespace (`schema:verb`)**:
   Set the button `action_id` to `"feedback:submit"` or `"order:submit"`. `CardCatalog.dispatch_action` strips the prefix and routes to `handle_action("submit", ...)` on the matching card class.
2. **Explicit payload (`schema`)**:
   Put `{"schema": "feedback", "action": "submit"}` in the button `data` / `value`. The gateway dispatches directly.
