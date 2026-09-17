import pytest

from agno_relay.core.streamui.schema import BlockSchema, CardCatalog, CardSchema, ItemSchema


class CustomMovieItem(ItemSchema):
    schema_name = "custom-movie"
    id: int
    note: str | None = None

    async def resolve(self, ctx=None):
        return {
            "title": f"The Matrix #{self.id}",
            "rating": 9.2,
            "year": 1999,
        }

    def render_teams(self, resolved):
        return {
            "type": "Container",
            "items": [{"type": "TextBlock", "text": f"Custom Teams: {resolved.get('title')}"}],
        }

    def render_lark(self, resolved):
        return {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**Custom Lark: {resolved.get('title')}**"},
        }

    def render_cli(self, resolved):
        return f"[Custom CLI: {resolved.get('title')}]"


class CustomMovieListBlock(BlockSchema):
    schema_name = "custom-movie-list"
    body = "items"
    item = CustomMovieItem

    def render_teams_block(self, rendered_items):
        return {
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [{"type": "TextBlock", "text": "Curated List"}, *rendered_items],
        }


class FallbackMovieItem(ItemSchema):
    schema_name = "fallback-movie"
    id: int
    genre: str = "Sci-Fi"
    note: str | None = None


class FallbackMovieListBlock(BlockSchema):
    schema_name = "fallback-movie-list"
    body = "items"
    item = FallbackMovieItem


class SingleHeroCard(CardSchema):
    schema_name = "hero-card"
    title: str
    status: str

    def render_teams(self, resolved):
        return {"type": "TextBlock", "text": f"Hero: {self.title} ({self.status})"}


@pytest.mark.asyncio
async def test_class_first_resolver_and_custom_renderers():
    catalog = CardCatalog([CustomMovieListBlock])

    # 1. Resolve facts through class-first method
    resolved = await catalog.resolve("custom-movie", {"id": 101, "note": "Must watch"})
    assert resolved == {
        "title": "The Matrix #101",
        "rating": 9.2,
        "year": 1999,
    }

    # 2. Render Teams custom fragment
    teams_item = catalog.render_item("teams", "custom-movie", {"id": 101}, resolved)
    assert teams_item == {
        "type": "Container",
        "items": [{"type": "TextBlock", "text": "Custom Teams: The Matrix #101"}],
    }

    # 3. Render Lark custom fragment
    lark_item = catalog.render_item("lark", "custom-movie", {"id": 101}, resolved)
    assert lark_item == {
        "tag": "div",
        "text": {"tag": "lark_md", "content": "**Custom Lark: The Matrix #101**"},
    }

    # 4. Render CLI custom fragment
    cli_item = catalog.render_item("cli", "custom-movie", {"id": 101}, resolved)
    assert cli_item == "[Custom CLI: The Matrix #101]"

    # 5. Render Teams custom block
    teams_block = catalog.render_block("teams", "custom-movie-list", {}, [teams_item])
    assert teams_block["type"] == "AdaptiveCard"
    assert teams_block["body"][0]["text"] == "Curated List"
    assert len(teams_block["body"]) == 2


def test_class_first_auto_fallback_teams_and_lark():
    catalog = CardCatalog([FallbackMovieListBlock])

    item_data = {"id": 42, "genre": "Sci-Fi", "note": "Great flick"}
    resolved = {"title": "Interstellar", "year": 2014}

    # 1. Teams fallback item
    teams_fallback = catalog.render_item("teams", "fallback-movie", item_data, resolved)
    assert teams_fallback["type"] == "Container"
    text_blocks = [x for x in teams_fallback["items"] if x["type"] == "TextBlock"]
    assert any("Interstellar" in x["text"] for x in text_blocks)
    assert any("Great flick" in x["text"] for x in text_blocks)
    facts = next(x for x in teams_fallback["items"] if x["type"] == "FactSet")
    fact_dict = {f["title"]: f["value"] for f in facts["facts"]}
    assert fact_dict["Genre"] == "Sci-Fi"
    assert fact_dict["Year"] == "2014"

    # 2. Teams fallback block
    teams_block = catalog.render_block(
        "teams", "fallback-movie-list", {"title": "Top Picks"}, [teams_fallback]
    )
    assert teams_block["type"] == "AdaptiveCard"
    assert teams_block["body"][0]["text"] == "Top Picks"
    assert teams_block["body"][1] == teams_fallback

    # 3. Lark fallback item
    lark_fallback = catalog.render_item("lark", "fallback-movie", item_data, resolved)
    assert lark_fallback["tag"] == "div"
    content = lark_fallback["text"]["content"]
    assert "**Interstellar**" in content
    assert "_Great flick_" in content
    assert "**Genre**: Sci-Fi" in content

    # 4. Lark fallback block
    lark_block = catalog.render_block(
        "lark", "fallback-movie-list", {"title": "Top Picks"}, [lark_fallback]
    )
    assert lark_block["schema"] == "2.0"
    assert lark_block["header"]["title"]["content"] == "Top Picks"
    assert lark_block["body"]["elements"] == [lark_fallback]


def test_card_schema_single_item():
    catalog = CardCatalog([SingleHeroCard])
    teams_out = catalog.render_item("teams", "hero-card", {"title": "Batman", "status": "Active"})
    assert teams_out == {"type": "TextBlock", "text": "Hero: Batman (Active)"}
