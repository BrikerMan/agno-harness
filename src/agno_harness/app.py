import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from ag_ui.core import EventType, RunAgentInput, UserMessage
from ag_ui.core.types import ToolMessage

from .channels.base import BaseChannel
from .core.attachment import AttachmentProcessor
from .core.channel import ChannelEvent, OutboundMessage
from .core.chimein import ChimeInPolicy
from .core.streamui.schema import CardCatalog
from .runtime.closure import strip_stream_ui
from .runtime.runtime import AgentRuntime
from .sessions.manager import SessionKeyResolver, SessionManager
from .sessions.models import ConversationKey
from .sinks.base import BaseSink
from .stream.buffer import ThrottledStreamBuffer
from .stream.collector import MessageCollector
from .stream.modes import StreamMode

log = logging.getLogger("agno_harness.app")

ActionHandler = Callable[[ChannelEvent], Awaitable[OutboundMessage | None]]


def _agui_text_delta(event: Any) -> str:
    event_type = getattr(event, "type", None)
    type_name = getattr(event_type, "name", "") or str(event_type)
    if event_type is EventType.TEXT_MESSAGE_CONTENT or type_name in {
        "TEXT_DELTA",
        "TEXT_MESSAGE_CONTENT",
    }:
        return getattr(event, "delta", "") or ""
    return ""


def _agui_reasoning_delta(event: Any) -> str:
    event_type = getattr(event, "type", None)
    type_name = getattr(event_type, "name", "") or str(event_type)
    if (
        event_type is EventType.REASONING_MESSAGE_CONTENT
        or "REASONING_MESSAGE_CONTENT" in type_name
    ):
        return getattr(event, "delta", "") or ""
    return ""


def _agui_reasoning_closed(event: Any) -> bool:
    event_type = getattr(event, "type", None)
    type_name = getattr(event_type, "name", "") or str(event_type)
    return event_type in {
        EventType.REASONING_END,
        EventType.REASONING_MESSAGE_END,
    } or type_name in {
        "REASONING_END",
        "REASONING_MESSAGE_END",
    }


def _agui_type_name(event: Any) -> str:
    event_type = getattr(event, "type", None)
    return getattr(event_type, "name", "") or str(event_type)


def _agui_tool_start(event: Any) -> tuple[str, str] | None:
    event_type = getattr(event, "type", None)
    if event_type is not EventType.TOOL_CALL_START and "TOOL_CALL_START" not in _agui_type_name(
        event
    ):
        return None
    tool_call_id = getattr(event, "tool_call_id", "") or ""
    name = getattr(event, "tool_call_name", None) or getattr(event, "tool_name", None) or "tool"
    return tool_call_id, name


def _agui_tool_args_delta(event: Any) -> tuple[str, str] | None:
    event_type = getattr(event, "type", None)
    if event_type is not EventType.TOOL_CALL_ARGS and "TOOL_CALL_ARGS" not in _agui_type_name(
        event
    ):
        return None
    return getattr(event, "tool_call_id", "") or "", getattr(event, "delta", "") or ""


def _agui_tool_end_id(event: Any) -> str | None:
    event_type = getattr(event, "type", None)
    if event_type is not EventType.TOOL_CALL_END and "TOOL_CALL_END" not in _agui_type_name(event):
        return None
    return getattr(event, "tool_call_id", "") or ""


def _parse_tool_args(raw: str) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class RelayApp:
    """Universal multi-channel production gateway consuming AG-UI streams from AgentRuntime."""

    def __init__(
        self,
        runtime: AgentRuntime,
        card_catalog: CardCatalog | None = None,
        session_manager: SessionManager | None = None,
        session_resolver: SessionKeyResolver | None = None,
        action_store: Any | None = None,
        attachment_processor: AttachmentProcessor | None = None,
        chime_in_policy: ChimeInPolicy | None = None,
        default_stream_mode: StreamMode = StreamMode.FINAL,
        enable_deduplication: bool = True,
        dedup_ttl_seconds: float = 300.0,
        action_handlers: Mapping[str, ActionHandler] | None = None,
    ) -> None:
        if not isinstance(runtime, AgentRuntime):
            raise TypeError(
                f"RelayApp requires an AgentRuntime instance, got {type(runtime).__name__}. "
                "Explicitly construct your runtime first: `runtime = AgentRuntime(agent=...)` "
                "then pass it to `RelayApp(runtime=runtime)`. "
                "This guarantees that the execution kernel strictly produces standardized AG-UI event streams."
            )

        self.runtime: AgentRuntime = runtime
        self.card_catalog = card_catalog or getattr(self.runtime, "catalog", None)
        self.action_store = action_store
        self.attachment_processor = attachment_processor
        self.chime_in_policy = chime_in_policy
        if session_manager is not None:
            self.session_manager = session_manager
            if session_resolver is not None:
                self.session_manager.resolver = session_resolver
        else:
            self.session_manager = SessionManager(resolver=session_resolver)

        self.default_stream_mode = default_stream_mode
        self.enable_deduplication = enable_deduplication
        self.dedup_ttl_seconds = dedup_ttl_seconds
        self._seen_events: dict[str, float] = {}

        self.channels: dict[str, tuple[BaseChannel, StreamMode]] = {}
        self.sinks: list[BaseSink] = []
        self._action_handlers: dict[str, ActionHandler] = dict(action_handlers or {})
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

    def register_action(self, action_id: str, handler: ActionHandler) -> "RelayApp":
        """Register a callback handler for interactive card actions (Teams / Lark buttons)."""
        self._action_handlers[action_id] = handler
        return self

    def action(self, action_id: str) -> Callable[[ActionHandler], ActionHandler]:
        """Decorator to register an action callback handler."""

        def decorator(handler: ActionHandler) -> ActionHandler:
            self.register_action(action_id, handler)
            return handler

        return decorator

    def get_router(self, **kwargs: Any) -> Any:
        """Create a unified FastAPI APIRouter bundling AG-UI routes and channel webhooks.

        Downstream enterprise applications mount this directly into their existing FastAPI app:
        ```python
        app.include_router(relay.get_router(resolve_user_id=my_auth))
        ```
        """
        from .transport.router import make_relay_router

        return make_relay_router(self, **kwargs)

    def as_server(self, **kwargs: Any) -> Any:
        """Create a class-based FastAPI RelayServer wrapping this application."""
        from .server.server import RelayServer

        return RelayServer(relay=self, **kwargs)

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Any = None) -> Any:
        """Async lifespan context manager for mounting into FastAPI applications.

        Usage:
        ```python
        app = FastAPI(lifespan=relay.lifespan)
        app.include_router(relay.get_router(resolve_user_id=...))
        ```
        """
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    async def handle_event(
        self, channel: BaseChannel, event: ChannelEvent
    ) -> OutboundMessage | None:
        """Route an inbound channel event through Agno runtime with idempotency and reaction safety."""
        text = event.text.strip()
        key = event.key

        # 0. Deduplicate incoming webhook retries by event_id
        if self.enable_deduplication and event.event_id:
            now_ts = time.time()
            if len(self._seen_events) > 1000:
                self._seen_events = {
                    eid: ts
                    for eid, ts in self._seen_events.items()
                    if now_ts - ts < self.dedup_ttl_seconds
                }
            if event.event_id in self._seen_events:
                log.warning(f"Dropping duplicate event {event.event_id} on channel {channel.name}")
                return OutboundMessage(text="")
            self._seen_events[event.event_id] = now_ts

        # 1. Handle interactive card actions (Agno HITL resume or custom callbacks)
        if event.action_id:
            # 1.1 Built-in Agno Human-in-the-Loop resume from Teams / Lark interactive cards
            if event.action_id == "agno.hitl.resume":
                payload = event.action_value or {} if isinstance(event.action_value, dict) else {}
                tool_call_id = payload.get("tool_call_id", "")
                pause_type = payload.get("pause_type", "confirmation")

                if pause_type == "confirmation":
                    accepted = payload.get("accepted", True)
                    content = json.dumps({"accepted": accepted})
                elif pause_type == "user_input":
                    content = json.dumps({"values": payload.get("values", {})})
                elif pause_type == "user_feedback":
                    content = json.dumps({"selections": payload.get("selections", {})})
                else:
                    content = json.dumps(payload.get("result", {}))

                tool_msg = ToolMessage(
                    id=str(uuid4()),
                    role="tool",
                    tool_call_id=tool_call_id,
                    content=content,
                )

                session_id, _ = await self.session_manager.get_or_create_session(key)

                # Persist action decision into SQLAlchemy action store if available
                decision = "accepted" if payload.get("accepted", True) else "rejected"
                if self.action_store:
                    with contextlib.suppress(Exception):
                        await self.action_store.update_status(
                            action_id=event.action_id,
                            status="approved" if decision == "accepted" else "rejected",
                            tool_call_id=tool_call_id or None,
                            result=payload,
                        )

                # Record CUSTOM frame into CustomEventStore for cross-platform visibility (e.g. Web replay)
                stores = getattr(self.runtime, "stores", None)
                if stores and getattr(stores, "custom_events", None):
                    with contextlib.suppress(Exception):
                        await stores.custom_events.save(
                            thread_id=session_id,
                            run_id=str(uuid4()),
                            name="action.resolved",
                            value={
                                "actionId": event.action_id,
                                "toolCallId": tool_call_id,
                                "decision": decision,
                                "userId": key.sender_id,
                                "platform": channel.name,
                                "meta": payload.get("meta") or {},
                                "timestamp": datetime.now(UTC).isoformat(),
                            },
                        )

                run_input = RunAgentInput(
                    thread_id=session_id,
                    run_id=str(uuid4()),
                    state={},
                    messages=[tool_msg],
                    tools=[],
                    context=[],
                    forwarded_props=None,
                )

                collector = MessageCollector(
                    platform=channel.name,
                    catalog=self.card_catalog,
                    session_id=session_id,
                    meta=payload.get("meta") or {},
                )
                ack_token = None
                with contextlib.suppress(Exception):
                    ack_token = await channel.ack(event)

                try:
                    meta = {
                        "platform": channel.name,
                        "chat_id": key.chat_id,
                        "thread_id": key.thread_id,
                        "sender_id": key.sender_id,
                        "tenant_id": key.tenant_id,
                    }
                    await self._drain_runtime_stream(
                        channel=channel,
                        key=key,
                        collector=collector,
                        events=self.runtime.stream_events(
                            run_input, user_id=key.sender_id, metadata=meta
                        ),
                        message_id=run_input.run_id,
                    )

                    outbound = collector.finalize()
                    if not outbound.text and not outbound.cards:
                        outbound.text = "Action confirmed and execution completed."
                    await channel.send(key, outbound)
                    with contextlib.suppress(Exception):
                        await channel.settle(key, ack_token, emoji="✅")
                    return outbound
                except Exception as exc:
                    log.exception(f"Error resuming Agno HITL for tool '{tool_call_id}': {exc}")
                    with contextlib.suppress(Exception):
                        await channel.settle(key, ack_token, emoji="❌")
                    err_out = OutboundMessage(text=f"Failed to resume execution: {exc}")
                    await channel.send(key, err_out)
                    return err_out

            payload = event.action_value or {} if isinstance(event.action_value, dict) else {}
            session_id, _ = await self.session_manager.get_or_create_session(key)

            # Audit inbound action event
            for sink in self.sinks:
                with contextlib.suppress(Exception):
                    await sink.record_inbound(event, session_id)

            # Shared ack token across card-catalog → app-handler fallback so we
            # never leave a reaction unsettled when nothing consumes the action.
            ack_token = None

            # 1.2 Class-First Card Action dispatch via self.card_catalog
            if self.card_catalog is not None:
                with contextlib.suppress(Exception):
                    ack_token = await channel.ack(event)
                try:
                    card_result = await self.card_catalog.dispatch_action(
                        event.action_id, payload, event
                    )
                    if card_result is not None:
                        # Record CUSTOM frame for Web UI synchronization if stores available
                        stores = getattr(self.runtime, "stores", None)
                        if stores and getattr(stores, "custom_events", None):
                            with contextlib.suppress(Exception):
                                await stores.custom_events.save(
                                    thread_id=session_id,
                                    run_id=str(uuid4()),
                                    name="action.executed",
                                    value={
                                        "actionId": event.action_id,
                                        "userId": key.sender_id,
                                        "platform": channel.name,
                                        "payload": payload,
                                        "timestamp": datetime.now(UTC).isoformat(),
                                    },
                                )

                        outbound = (
                            card_result
                            if isinstance(card_result, OutboundMessage)
                            else OutboundMessage(text=str(card_result))
                        )
                        settle_emoji = (outbound.extra or {}).get("settle_emoji", "✅")
                        if outbound.text or outbound.cards:
                            await channel.send(key, outbound)
                        with contextlib.suppress(Exception):
                            await channel.settle(key, ack_token, emoji=settle_emoji)
                        return outbound
                except Exception as exc:
                    log.exception(f"Error in Class-First card action '{event.action_id}': {exc}")
                    with contextlib.suppress(Exception):
                        await channel.settle(key, ack_token, emoji="❌")
                    err_outbound = OutboundMessage(text=f"Failed to process card action: {exc}")
                    await channel.send(key, err_outbound)
                    return err_outbound

            # 1.3 Bot-level / App-level action handlers (passed via constructor or register_action)
            # Used for standalone actions (e.g. 点赞, 收藏, 独立回调) without LLM invocation.
            handler = self._action_handlers.get(event.action_id)
            if handler:
                if ack_token is None:
                    with contextlib.suppress(Exception):
                        ack_token = await channel.ack(event)
                try:
                    result = await handler(event)

                    # Record CUSTOM frame for Web UI synchronization if stores available
                    stores = getattr(self.runtime, "stores", None)
                    if stores and getattr(stores, "custom_events", None):
                        with contextlib.suppress(Exception):
                            await stores.custom_events.save(
                                thread_id=session_id,
                                run_id=str(uuid4()),
                                name="action.executed",
                                value={
                                    "actionId": event.action_id,
                                    "userId": key.sender_id,
                                    "platform": channel.name,
                                    "payload": payload,
                                    "timestamp": datetime.now(UTC).isoformat(),
                                },
                            )

                    if result is None:
                        # Silent action (e.g. thumbs-up/like): settle reaction without chat bubble spam
                        with contextlib.suppress(Exception):
                            await channel.settle(key, ack_token, emoji="✅")
                        return OutboundMessage(text="")

                    outbound = (
                        result
                        if isinstance(result, OutboundMessage)
                        else OutboundMessage(text=str(result))
                    )
                    settle_emoji = (outbound.extra or {}).get("settle_emoji", "✅")
                    if outbound.text or outbound.cards:
                        await channel.send(key, outbound)
                    with contextlib.suppress(Exception):
                        await channel.settle(key, ack_token, emoji=settle_emoji)
                    return outbound
                except Exception as exc:
                    log.exception(f"Error handling card action '{event.action_id}': {exc}")
                    with contextlib.suppress(Exception):
                        await channel.settle(key, ack_token, emoji="❌")
                    err_outbound = OutboundMessage(text=f"Failed to process action: {exc}")
                    await channel.send(key, err_outbound)
                    return err_outbound

            # Unhandled card action: never fall through into a normal agent turn
            # (empty text would still burn LLM tokens and leave ack unsettled).
            log.warning(
                f"Unhandled card action '{event.action_id}' on channel {channel.name}; "
                "dropping without agent invocation"
            )
            with contextlib.suppress(Exception):
                if ack_token is not None:
                    await channel.settle(key, ack_token, emoji="❓")
            return OutboundMessage(text="")

        # 2. Check Chime-In Policy for group/channel messages (least privilege guardrail)
        if self.chime_in_policy is not None:
            should_chime_in = await self.chime_in_policy.should_chime_in(event)
            if not should_chime_in:
                log.debug(
                    f"Chime-in policy suppressed execution for event {event.event_id} in {key.chat_id}"
                )
                return None

        # 3. Handle session-level control commands (/reset, /new)
        if text.lower() in ("/reset", "/new", "/clear"):
            await self.session_manager.close_session(key, reason="user_command")
            outbound = OutboundMessage(
                text="Conversation context has been cleared. Starting fresh!"
            )
            await channel.send(key, outbound)
            return outbound

        # 4. Resolve session key (with user-defined resolver)
        session_id, is_new = await self.session_manager.get_or_create_session(key)

        # 5. Audit inbound event
        for sink in self.sinks:
            try:
                await sink.record_inbound(event, session_id)
            except Exception as exc:
                log.warning(f"Error in sink record_inbound: {exc}")

        # 6. Instant Reaction ACK & Typing indicator (safe execution)
        ack_token = None
        with contextlib.suppress(Exception):
            ack_token = await channel.ack(event)
        with contextlib.suppress(Exception):
            await channel.typing(key, True)

        try:
            meta = cast(
                dict[str, Any],
                {
                    "platform": channel.name,
                    "chat_id": key.chat_id,
                    "thread_id": key.thread_id,
                    "sender_id": key.sender_id,
                    "tenant_id": key.tenant_id,
                    "sender_name": event.sender_name,
                    "sent_at": event.created_at.isoformat() if event.created_at else None,
                    "is_direct_message": key.is_direct_message,
                },
            )

            # 7. Pluggable attachment processing (OCR, DocMind, file extraction)
            effective_prompt = text
            context_items: list[Any] = []
            if self.attachment_processor is not None and event.attachments:
                try:
                    att_ctx = await self.attachment_processor.process(event.attachments, event)
                    if att_ctx:
                        context_items.append(
                            {"description": "Attached documents", "value": att_ctx}
                        )
                        meta["attachments_text"] = att_ctx
                except Exception as exc:
                    log.warning(f"Error in attachment_processor: {exc}")

            # 8. Execute Agent reasoning and stream UI events
            collector = MessageCollector(
                platform=channel.name,
                catalog=self.card_catalog,
                session_id=session_id,
                meta=meta,
            )
            run_input = RunAgentInput(
                thread_id=session_id,
                run_id=str(uuid4()),
                state={},
                messages=[UserMessage(id=str(uuid4()), role="user", content=effective_prompt)],
                tools=[],
                context=context_items,
                forwarded_props=None,
            )

            await self._drain_runtime_stream(
                channel=channel,
                key=key,
                collector=collector,
                events=self.runtime.stream_events(run_input, user_id=key.sender_id, metadata=meta),
                message_id=run_input.run_id,
            )

            # 7. Finalize outbound message & aggregated cards
            outbound = collector.finalize()

            # Persist pending actions into SQLAlchemy action store if available
            if self.action_store and collector.paused_descriptors:
                for desc in collector.paused_descriptors:
                    with contextlib.suppress(Exception):
                        await self.action_store.create_action(
                            action_id="agno.hitl.resume",
                            session_key=key.session_key,
                            agno_session_id=session_id,
                            payload=desc,
                            run_id=run_input.run_id,
                            tool_call_id=desc.get("toolCallId"),
                            meta=meta,
                        )

            # Guard against completely empty generation
            if not outbound.text and not outbound.cards:
                outbound.text = "I completed the request, but have no additional text to display."

            # 8. Deliver to channel & settle reaction ACK
            await channel.send(key, outbound)
            await channel.settle(key, ack_token, emoji="✅")

            # 9. Audit outbound response
            for sink in self.sinks:
                try:
                    await sink.record_outbound(key, outbound, session_id)
                except Exception as exc:
                    log.warning(f"Error in sink record_outbound: {exc}")

            return outbound
        except Exception as exc:
            log.exception(f"Unhandled error while executing turn for {key}: {exc}")
            # Ensure ghost reaction is resolved to ❌
            with contextlib.suppress(Exception):
                await channel.settle(key, ack_token, emoji="❌")
            err_msg = OutboundMessage(text="抱歉，处理您的请求时遇到了问题，请稍后重试。")
            with contextlib.suppress(Exception):
                await channel.send(key, err_msg)
            return err_msg
        finally:
            with contextlib.suppress(Exception):
                await channel.typing(key, False)

    async def _drain_runtime_stream(
        self,
        *,
        channel: BaseChannel,
        key: ConversationKey,
        collector: MessageCollector,
        events: AsyncIterator[Any],
        message_id: str,
    ) -> None:
        """Feed AG-UI events into the collector and optionally push live deltas."""
        mode = self.channels.get(channel.name, (channel, self.default_stream_mode))[1]
        visible = ""
        raw_parts: list[str] = []
        tool_args: dict[str, str] = {}
        tool_names: dict[str, str] = {}
        throttle: ThrottledStreamBuffer | None = None
        if mode is StreamMode.THROTTLE:
            throttle = ThrottledStreamBuffer(
                lambda text: channel.stream_chunk(key, message_id, text)
            )

        async def emit(text: str) -> None:
            if mode is StreamMode.RAW:
                await channel.stream_chunk(key, message_id, text)
            elif throttle is not None:
                await throttle.append(text)

        async def emit_tool(tool_call_id: str, name: str, args: Any, status: str) -> None:
            if mode is StreamMode.FINAL:
                return
            await channel.stream_tool(
                key,
                tool_call_id=tool_call_id,
                name=name,
                args=args,
                status=status,
            )

        async for agui_event in events:
            collector.feed(agui_event)
            if mode is StreamMode.FINAL:
                continue
            started = _agui_tool_start(agui_event)
            if started is not None:
                tool_call_id, name = started
                tool_names[tool_call_id] = name
                tool_args.setdefault(tool_call_id, "")
                await emit_tool(tool_call_id, name, None, "started")
                continue
            args_delta = _agui_tool_args_delta(agui_event)
            if args_delta is not None:
                tool_call_id, delta = args_delta
                tool_args[tool_call_id] = tool_args.get(tool_call_id, "") + delta
                parsed = _parse_tool_args(tool_args[tool_call_id])
                if parsed is not None:
                    await emit_tool(
                        tool_call_id,
                        tool_names.get(tool_call_id, "tool"),
                        parsed,
                        "args",
                    )
                continue
            ended_id = _agui_tool_end_id(agui_event)
            if ended_id is not None:
                await emit_tool(
                    ended_id,
                    tool_names.get(ended_id, "tool"),
                    _parse_tool_args(tool_args.get(ended_id, "")),
                    "ended",
                )
                continue
            reasoning = _agui_reasoning_delta(agui_event)
            if reasoning:
                await channel.stream_reasoning(key, reasoning)
                continue
            if _agui_reasoning_closed(agui_event):
                await channel.stream_reasoning(key, "", done=True)
                continue
            delta = _agui_text_delta(agui_event)
            if not delta:
                continue
            raw_parts.append(delta)
            clean = strip_stream_ui("".join(raw_parts))
            extra = clean[len(visible) :] if clean.startswith(visible) else clean
            visible = clean
            if extra:
                await emit(extra)

        if throttle is not None:
            await throttle.flush()

    async def _channel_worker(self, channel: BaseChannel) -> None:
        """Background listening loop for an individual channel."""
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
        """Start listening across all registered channels concurrently.

        Channels are started before this returns so callers can immediately
        enter an interactive loop (``CLIChannel.run_interactive_loop``).
        """
        self._running = True
        for channel, _ in self.channels.values():
            await channel.start()
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
        for channel, _ in self.channels.values():
            await channel.stop()
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
