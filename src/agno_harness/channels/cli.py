import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.pretty import Pretty
from rich.status import Status
from rich.syntax import Syntax
from rich.text import Text

from ..core.channel import ChannelEvent, OutboundMessage
from ..sessions.models import ConversationKey
from .base import BaseChannel


class CLIChannel(BaseChannel):
    """Terminal interactive channel powered by Rich."""

    def __init__(self, console: Console | None = None) -> None:
        super().__init__(name="cli")
        self.console = console or Console()
        self._interactive_task: asyncio.Task[None] | None = None
        self._streamed_text = False
        self._turn_has_output = False
        self._status: Status | None = None
        self._live: Live | None = None
        self._live_kind: str | None = None
        self._stream_buf: list[str] = []
        self._reasoning_buf: list[str] = []
        self._shown_tools: set[str] = set()
        self._turn_done = asyncio.Event()
        self._turn_done.set()

    async def start(self) -> None:
        await super().start()

    async def run_interactive_loop(
        self, chat_id: str = "local_cli", sender_id: str = "developer"
    ) -> None:
        """Run an interactive CLI read-eval-print loop."""
        if not self._running:
            await self.start()
        self.console.print(
            Panel.fit(
                "[bold]agno-harness[/bold]  [green]CLI ready[/green]\n"
                "[dim]Type to chat · /reset new session · /exit quit[/dim]",
                border_style="green",
            )
        )
        loop = asyncio.get_running_loop()

        while self._running:
            await self._turn_done.wait()
            try:
                self.console.print()
                self.console.print("[bold green]You[/bold green] [dim]›[/dim] ", end="")
                file = getattr(self.console, "file", None)
                if file is not None:
                    file.flush()
                user_input = await loop.run_in_executor(None, lambda: input().strip())
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            if user_input.lower() in ("/exit", "/quit"):
                self.console.print("[dim]Bye.[/dim]")
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
                created_at=datetime.now(UTC),
                sender_name=sender_id,
            )
            self._turn_done.clear()
            await self.push_event(event)

    def _use_live(self) -> bool:
        return bool(self.console.is_terminal) and not self.console.record

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def _start_status(self, message: str) -> None:
        self._stop_status()
        self._status = self.console.status(message, spinner="dots")
        self._status.start()

    def _assistant_panel(self, text: str, *, streaming: bool) -> Panel:
        body: RenderableType = Markdown(text) if text.strip() else Text("…", style="dim")
        return Panel(
            body,
            title="[bold cyan]Assistant[/bold cyan]",
            subtitle="[dim]streaming[/dim]" if streaming else None,
            border_style="cyan",
            padding=(0, 1),
        )

    def _reasoning_panel(self, text: str, *, streaming: bool) -> Panel:
        body = Text(text or "…", style="dim italic")
        return Panel(
            body,
            title="[bold yellow]Reasoning[/bold yellow]",
            subtitle="[dim]thinking[/dim]" if streaming else None,
            border_style="yellow",
            padding=(0, 1),
        )

    def _finish_live(self) -> None:
        if self._live is None:
            return
        if self._live_kind == "reasoning":
            text = "".join(self._reasoning_buf)
            self._live.update(self._reasoning_panel(text, streaming=False))
            self._reasoning_buf.clear()
        else:
            text = "".join(self._stream_buf)
            self._live.update(self._assistant_panel(text, streaming=False))
            self._stream_buf.clear()
            self._streamed_text = False
        self._live.stop()
        self._live = None
        self._live_kind = None

    def _flush_reasoning_record(self) -> None:
        if self._use_live() or not self._reasoning_buf:
            return
        self.console.print(self._reasoning_panel("".join(self._reasoning_buf), streaming=False))
        self._reasoning_buf.clear()

    def _update_live(self, kind: str, panel: Panel) -> None:
        if self._live is not None and self._live_kind != kind:
            self._finish_live()
        if self._live is None:
            self._live = Live(panel, console=self.console, refresh_per_second=16, transient=False)
            self._live.start()
            self._live_kind = kind
            return
        self._live.update(panel)

    async def stream_chunk(self, destination: ConversationKey, message_id: str, delta: str) -> None:
        """Typewriter-print a live token delta."""
        if not delta:
            return
        self._flush_reasoning_record()
        if self._live_kind == "reasoning":
            self._finish_live()
        self._stop_status()
        self._turn_has_output = True
        self._streamed_text = True
        self._stream_buf.append(delta)
        text = "".join(self._stream_buf)
        if self._use_live():
            self._update_live("assistant", self._assistant_panel(text, streaming=True))
            return
        self.console.print(delta, end="", highlight=False, markup=False)
        file = getattr(self.console, "file", None)
        if file is not None:
            file.flush()

    async def stream_reasoning(
        self, destination: ConversationKey, delta: str, *, done: bool = False
    ) -> None:
        """Render model thinking in a dedicated Reasoning panel."""
        if done:
            self._flush_reasoning_record()
            if self._live_kind == "reasoning":
                self._finish_live()
            return
        if not delta:
            return
        self._stop_status()
        self._turn_has_output = True
        self._reasoning_buf.append(delta)
        text = "".join(self._reasoning_buf)
        if self._use_live():
            self._update_live("reasoning", self._reasoning_panel(text, streaming=True))
            return
        self.console.print(delta, end="", style="dim italic", highlight=False, markup=False)
        file = getattr(self.console, "file", None)
        if file is not None:
            file.flush()

    async def stream_tool(
        self,
        destination: ConversationKey,
        *,
        tool_call_id: str,
        name: str,
        args: Any = None,
        status: str = "started",
    ) -> None:
        """Render a tool call as a Rich panel, including params when available."""
        self._flush_reasoning_record()
        self._finish_live()
        if status == "started" and args is None:
            self._start_status(f"[magenta]Calling [bold]{name}[/bold]…[/magenta]")
            return
        if tool_call_id in self._shown_tools:
            return
        self._shown_tools.add(tool_call_id)
        self._stop_status()
        self._turn_has_output = True
        self.console.print(self._tool_panel(name, args))

    def _tool_panel(self, name: str, args: Any) -> Panel:
        if args is None:
            body: RenderableType = Text("no params", style="dim")
        elif isinstance(args, dict):
            try:
                body = Syntax(
                    json.dumps(args, indent=2, ensure_ascii=False),
                    "json",
                    theme="ansi_dark",
                    word_wrap=True,
                )
            except TypeError:
                body = Pretty(args)
        else:
            body = Text(str(args))
        return Panel(
            Group(Text(name, style="bold magenta"), body),
            title="[bold magenta]Tool[/bold magenta]",
            border_style="magenta",
            padding=(0, 1),
            expand=False,
        )

    async def send(self, destination: ConversationKey, message: OutboundMessage) -> str:
        """Render outgoing response into the terminal."""
        self._stop_status()
        if self._live is not None:
            if message.text:
                self._stream_buf = [message.text]
            self._finish_live()
        elif self._streamed_text:
            self.console.print()
            self._streamed_text = False
            self._stream_buf.clear()
        elif message.text:
            self.console.print(self._assistant_panel(message.text, streaming=False))
            self._turn_has_output = True

        if message.cards:
            self._turn_has_output = True
            for card in message.cards:
                if isinstance(card, str):
                    self.console.print(Panel(card, title="Card", border_style="yellow"))
                elif isinstance(card, dict):
                    title = (
                        card.get("title")
                        or card.get("header", {}).get("title", {}).get("content")
                        or "Card"
                    )
                    self.console.print(Panel(str(card), title=str(title), border_style="yellow"))

        self._turn_done.set()
        return f"cli_msg_{destination.chat_id}"

    async def typing(self, destination: ConversationKey, active: bool) -> None:
        if active:
            self._finish_live()
            self._streamed_text = False
            self._turn_has_output = False
            self._stream_buf.clear()
            self._reasoning_buf.clear()
            self._shown_tools.clear()
            self._start_status("[yellow]Thinking…[/yellow]")
            return
        if self._turn_has_output:
            self._stop_status()
            self._turn_done.set()
            return
        self._stop_status()
        self._turn_done.set()
