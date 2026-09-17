import asyncio

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey
from .base import BaseChannel


class CLIChannel(BaseChannel):
    """Terminal interactive channel powered by Rich."""

    def __init__(self, console: Console | None = None) -> None:
        super().__init__(name="cli")
        self.console = console or Console()
        self._interactive_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await super().start()

    async def run_interactive_loop(
        self, chat_id: str = "local_cli", sender_id: str = "developer"
    ) -> None:
        """Run an interactive CLI read-eval-print loop."""
        self.console.print(
            "[bold green]agno-relay CLI Channel active. Type '/exit' to quit.[/bold green]"
        )
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                user_input = await loop.run_in_executor(None, lambda: input("\nYou > ").strip())
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            if user_input.lower() in ("/exit", "/quit"):
                self.console.print("[dim]Exiting CLI...[/dim]")
                break

            event = ChannelEvent(
                event_id=f"cli_{asyncio.get_event_loop().time()}",
                key=ConversationKey(
                    platform="cli",
                    chat_id=chat_id,
                    sender_id=sender_id,
                    is_direct_message=True,
                ),
                text=user_input,
            )
            await self.push_event(event)

    async def send(self, destination: ConversationKey, message: OutboundMessage) -> str:
        """Render outgoing response into the terminal."""
        if message.text:
            self.console.print()
            self.console.print(Markdown(message.text))

        if message.cards:
            for card in message.cards:
                if isinstance(card, str):
                    self.console.print(Panel(card, title="Card", border_style="cyan"))
                elif isinstance(card, dict):
                    title = (
                        card.get("title")
                        or card.get("header", {}).get("title", {}).get("content")
                        or "Card"
                    )
                    self.console.print(Panel(str(card), title=str(title), border_style="cyan"))

        return f"cli_msg_{destination.chat_id}"

    async def typing(self, destination: ConversationKey, active: bool) -> None:
        if active:
            self.console.print("[dim italic]Thinking...[/dim italic]", end="\r")
        else:
            self.console.print(" " * 20, end="\r")
