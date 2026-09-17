"""CLI Demo for agno-relay.

Run directly in terminal:
    uv run python examples/01_cli_demo.py

Features demonstrated:
1. Pure local execution without network ingress.
2. Rich interactive terminal interface.
3. Class-First CardCatalog integration (N items -> 1 aggregate CLI card).
4. Session reset command (/reset).
"""

import asyncio
from typing import Any
from rich.console import Console

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno_relay import (
    BlockSchema,
    CardCatalog,
    CLIChannel,
    ItemSchema,
    RelayApp,
    SQLiteSink,
)


# 1. Define a Class-First Card Component
class MovieItem(ItemSchema):
    """Movie recommendation item."""
    schema_name = "movie"
    id: int
    note: str | None = None

    async def resolve(self, ctx: Any = None) -> dict[str, Any]:
        # Server-side authoritative facts
        db = {
            1: {"title": "Inception", "year": 2010, "rating": "★ 8.8"},
            2: {"title": "Interstellar", "year": 2014, "rating": "★ 8.7"},
            3: {"title": "The Matrix", "year": 1999, "rating": "★ 8.7"},
        }
        return db.get(self.id, {"title": f"Movie #{self.id}", "year": 2024, "rating": "★ 8.0"})

    def render_cli(self, resolved: dict[str, Any]) -> str:
        return f"🎬 [bold cyan]{resolved.get('title')}[/bold cyan] ({resolved.get('year')}) · [yellow]{resolved.get('rating')}[/yellow]\n   Note: {self.note or 'No notes'}"


class MovieListBlock(BlockSchema):
    """Container block holding multiple movies."""
    schema_name = "movie-list"
    body = "items"
    item = MovieItem
    title: str | None = None

    def render_cli_block(self, rendered_items: list[str]) -> str:
        items_text = "\n\n".join(rendered_items)
        header = f"🍿 [bold magenta]{self.title or 'Curated Movies'}[/bold magenta]"
        return f"{header}\n{'─' * 40}\n{items_text}"


catalog = CardCatalog([MovieListBlock])


# 2. Build the AGNO Agent with the catalog prompt
system_instructions = (
    "You are a movie recommendation assistant.\n"
    "When asked for movies, use the following card UI format:\n\n"
    f"{catalog.to_prompt()}"
)

# Use OpenAI or compatible endpoint if env var is set, else fall back to a mock model for offline tests
try:
    import os
    if os.getenv("OPENAI_API_KEY"):
        model = OpenAIChat(id="gpt-4o-mini")
    else:
        # Fallback offline agent for demo purposes when no API key is provided
        model = None
except Exception:
    model = None


class OfflineDemoAgent:
    """Mock agent that can run without external API keys."""
    def __init__(self, instructions: str):
        self.instructions = instructions

    async def arun(self, *args, **kwargs):
        # We can yield simulated responses
        from agno.run.agent import RunContentEvent, RunCompletedEvent
        yield RunContentEvent(
            content=(
                "I found some awesome sci-fi movies for you!\n\n"
                "```stream-ui {\"schema\": \"movie-list\", \"title\": \"Top Sci-Fi Picks\"}\n"
                "{\"id\": 1, \"note\": \"Mind-bending dreams within dreams\"}\n"
                "{\"id\": 2, \"note\": \"Epic journey through a wormhole\"}\n"
                "```\n\n"
                "Type /reset anytime to clear the session."
            )
        )
        yield RunCompletedEvent()


agent = Agent(name="MovieBot", model=model, instructions=system_instructions) if model else OfflineDemoAgent(system_instructions)

# 3. Mount onto RelayApp chassis
app = RelayApp(agent, card_catalog=catalog)

# Add CLI Channel
cli_channel = CLIChannel()
app.add_channel(cli_channel)

# Add Audit Sink (stores message history locally in SQLite)
app.add_sink(SQLiteSink(db_path="data/cli_demo_audit.db"))


async def main():
    console = Console()
    console.print("[bold green]Starting agno-relay CLI Interactive Demo...[/bold green]")
    if not model:
        console.print("[yellow](Note: OPENAI_API_KEY not found; running in offline simulation mode)[/yellow]")

    # Start relay background worker
    await app.start()

    try:
        # Run CLI read-eval-print loop
        await cli_channel.run_interactive_loop(chat_id="local_dev", sender_id="developer")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
