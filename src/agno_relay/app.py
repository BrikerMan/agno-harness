import asyncio
import contextlib
import logging
from typing import Any
from uuid import uuid4

from ag_ui.core import RunAgentInput, UserMessage

from .channels.base import BaseChannel
from .core.channel import ChannelEvent, OutboundMessage
from .core.streamui.schema import CardCatalog
from .runtime.runtime import AguiRuntime
from .sessions.manager import SessionManager
from .sinks.base import BaseSink
from .stream.collector import MessageCollector
from .stream.modes import StreamMode

log = logging.getLogger("agno_relay.app")


class RelayApp:
    """Universal production gateway application mounting AGNO agents across multiple channels."""

    def __init__(
        self,
        agent_or_runtime: Any,
        card_catalog: CardCatalog | None = None,
        session_manager: SessionManager | None = None,
        default_stream_mode: StreamMode = StreamMode.FINAL,
    ) -> None:
        if isinstance(agent_or_runtime, AguiRuntime):
            self.runtime = agent_or_runtime
        else:
            # Wrap standard Agno Agent in AguiRuntime
            self.runtime = AguiRuntime(agent=agent_or_runtime, catalog=card_catalog)

        self.card_catalog = card_catalog or getattr(self.runtime, "catalog", None)
        self.session_manager = session_manager or SessionManager()
        self.default_stream_mode = default_stream_mode

        self.channels: dict[str, tuple[BaseChannel, StreamMode]] = {}
        self.sinks: list[BaseSink] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    def add_channel(
        self, channel: BaseChannel, stream_mode: StreamMode | None = None
    ) -> "RelayApp":
        """Register a transport channel adapter (Web, Teams, Lark, CLI)."""
        mode = stream_mode or (
            StreamMode.RAW if channel.name in ("web", "cli") else self.default_stream_mode
        )
        self.channels[channel.name] = (channel, mode)
        return self

    def add_sink(self, sink: BaseSink) -> "RelayApp":
        """Register a message audit sink (e.g. SQLiteSink, PostgresSink)."""
        self.sinks.append(sink)
        return self

    async def handle_event(self, channel: BaseChannel, event: ChannelEvent) -> OutboundMessage:
        """Route an inbound channel event through Agno runtime and return normalized response."""
        text = event.text.strip()
        key = event.key

        # 1. Handle session-level control commands (/reset, /new)
        if text.lower() in ("/reset", "/new", "/clear"):
            await self.session_manager.close_session(key, reason="user_command")
            outbound = OutboundMessage(
                text="Conversation context has been cleared. Starting fresh!"
            )
            await channel.send(key, outbound)
            return outbound

        # 2. Resolve Ivy-grade session key
        session_id, is_new = await self.session_manager.get_or_create_session(key)

        # 3. Audit inbound event
        for sink in self.sinks:
            try:
                await sink.record_inbound(event, session_id)
            except Exception as exc:
                log.warning(f"Error in sink record_inbound: {exc}")

        # 4. Instant Reaction ACK & Typing indicator
        ack_token = await channel.ack(event)
        await channel.typing(key, True)

        # 5. Execute Agent reasoning and stream UI events
        collector = MessageCollector(platform=channel.name, catalog=self.card_catalog)
        run_input = RunAgentInput(
            thread_id=session_id,
            run_id=str(uuid4()),
            state={},
            messages=[UserMessage(id=str(uuid4()), role="user", content=text)],
            tools=[],
            context=[],
            forwarded_props=None,
        )

        try:
            async for agui_event in self.runtime.stream_events(run_input):
                collector.feed(agui_event)
        finally:
            await channel.typing(key, False)

        # 6. Finalize outbound message & aggregated cards
        outbound = collector.finalize()

        # 7. Deliver to channel & settle reaction ACK
        await channel.send(key, outbound)
        await channel.settle(key, ack_token)

        # 8. Audit outbound response
        for sink in self.sinks:
            try:
                await sink.record_outbound(key, outbound, session_id)
            except Exception as exc:
                log.warning(f"Error in sink record_outbound: {exc}")

        return outbound

    async def _channel_worker(self, channel: BaseChannel) -> None:
        """Background listening loop for an individual channel."""
        await channel.start()
        try:
            async for event in channel.listen():
                if not self._running:
                    break
                asyncio.create_task(self.handle_event(channel, event))
        except asyncio.CancelledError:
            pass
        finally:
            await channel.stop()

    async def start(self) -> None:
        """Start listening across all registered channels concurrently."""
        self._running = True
        for channel, _ in self.channels.values():
            task = asyncio.create_task(self._channel_worker(channel))
            self._tasks.append(task)
        log.info(f"RelayApp started with channels: {list(self.channels.keys())}")

    async def stop(self) -> None:
        """Gracefully shut down all channel workers."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("RelayApp stopped.")

    def serve(self) -> None:
        """Blocking synchronous entrypoint to run the application event loop."""

        async def _main():
            await self.start()
            try:
                while self._running:
                    await asyncio.sleep(1)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                await self.stop()

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_main())
