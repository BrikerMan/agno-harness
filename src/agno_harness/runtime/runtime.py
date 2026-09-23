"""AgentRuntime — the batteries-included assembly.

WHY OWN THE LOOP
----------------
Agno ships an AG-UI interface as part of AgentOS, but it is a closed box: input
goes in, SSE comes out, and there is nowhere to stand in between. Owning the loop
buys the things a closed box cannot give you — injecting events before a run,
patching the official converter where it drops fields, hiding internal tools,
streaming a sub-agent's inner work, extracting generative-UI blocks out of the
text stream.

What it does *not* buy, and must not cost, is the chunk-to-event mapping itself.
That mapping changes with every Agno release, so the runtime reuses Agno's own
per-chunk ``HANDLERS`` and ``process_completion`` rather than reimplementing
them, and layers extensions on top.

THREE WAYS TO USE THIS
----------------------
*Fully managed* — ``runtime.stream_events(run_input)``. One call, well-formed
AG-UI out.

*Semi-managed* — the same, plus hooks. A pre-run hook receives the mutable
:class:`~agno_harness.runtime.scope.RunScope` and may set ``user_id``,
seed the shared state document, add keyword arguments for ``agent.arun()``, and
yield events. A post-run hook sees the agent's terminal chunk and may yield
events of its own before the run closes.

*Fully manual* — skip this class. Build a scope, drive
:class:`~agno_harness.runtime.translator.EventTranslator` yourself, call
``agent.arun()`` however you like. Nothing below the transport package imports
FastAPI, so that path works in a script or a worker.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from ag_ui.core import BaseEvent, EventType, RunAgentInput
from ag_ui.encoder import EventEncoder
from agno.os.interfaces.agui.input import validate_state
from agno.os.interfaces.agui.state import StreamState
from agno.run.agent import RunEvent

from ..config import RelayConfig
from ..core.prompt import UserQueryBuilder, default_builder
from ..core.sequencer import ProtocolViolation, SequencerMode
from ..core.streamui import CardCatalog
from ..core.types import ToolFilter
from ..stores.prefix import apply_table_prefix
from ..stores.registry import Stores
from .closure import seal_session_run
from .hitl import detect_resume, resume_result_events
from .inspector import InspectorRegistry
from .module import BridgeModule, ModuleRegistry
from .modules.custom_events import CustomEventsModule
from .modules.streamui import StreamUIModule
from .modules.subagent import SubAgentModule
from .modules.tool_filters import ToolFilterModule
from .parsers import compression_events_parser, reasoning_content_parser
from .replay import last_user_text
from .runner import AgentRunner
from .scope import RunScope
from .state import StateTracker
from .storage_guard import agno_db_is_persistent, check_history_pairing
from .threads import ThreadService
from .titles import EVENT_THREAD_TITLE
from .titles import generate_thread_title as _generate_thread_title
from .tracing import RunTracer
from .translator import EVENT_RUN_CANCELLED, EVENT_RUN_PAUSED, AgentRunFailed, EventTranslator
from .types import EventParser, PostRunHook, PreRunHook


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class AgentRuntime:
    """Core runtime that drives an Agno agent and manages the AG-UI event lifecycle.

    Parameters
    ----------
    agent:
        Any Agno ``Agent`` or ``Team``.
    db:
        Agno session database. Required for history replay and for HITL resume
        (a paused run has to be found again).
    stores:
        Optional :class:`~agno_harness.stores.Stores` for records Agno
        does not keep, chiefly injected ``CUSTOM`` events. Without it those
        events are live-only and will not survive a reload.
    allow_ephemeral_agno_db:
        Durable SQL harness stores require a persistent Agno ``db``. Set this
        to opt into the half-pair (UI replay, model amnesia) — tests only.
    enable_reasoning_patch:
        Register the built-in reasoning parser, so thinking models that put
        their chain of thought on ``reasoning_content`` surface it live.
    enable_subagent_streaming:
        Let tools forward a sub-agent's chunks into this run via ``substream``.
    catalog:
        The StreamUI card schemas this agent may render. Without one, blocks are
        passed through unvalidated and no resolver runs — fine while
        prototyping, not something to ship, since the prompt, the server and the
        frontend then have nothing keeping them in agreement.
    enable_streamui:
        Extract ``stream-ui`` fences from the text stream.
    sequencer_mode:
        ``repair`` in production. ``audit`` records what was repaired (the debug
        UI reads it); ``strict`` raises, which is what the tests use.
    record_chunks:
        Raw chunk samples to keep per event type, per run; ``0`` disables
        recording.
    trace_dir:
        Write a per-run JSONL of the chunk-to-event mapping here. Defaults to
        ``$AGNO_HARNESS_TRACE_DIR``, and to off when that is unset. See
        :mod:`agno_harness.runtime.tracing`.
    user_query_builder:
        Override the default :class:`UserQueryBuilder` (plugins / timezone).
        Every run wraps the latest user turn as ``<user-query>`` plus
        ``<context>`` (time, speaker, channel) before ``agent.arun``.
    """

    def __init__(
        self,
        agent: Any,
        db: Any = None,
        *,
        harness_db: Any = None,
        stores: Stores | None = None,
        allow_ephemeral_agno_db: bool = False,
        catalog: CardCatalog | None = None,
        enable_reasoning_patch: bool = True,
        enable_compression_streaming: bool = True,
        enable_subagent_streaming: bool = True,
        enable_streamui: bool = True,
        artifact_root_dir: str | os.PathLike[str] | Callable[[RunScope], Path | str] | None = None,
        workspace_dir: str | os.PathLike[str] | Callable[[RunScope], Path | str] | None = None,
        sequencer_mode: SequencerMode = SequencerMode.REPAIR,
        record_chunks: int = 3,
        trace_dir: str | os.PathLike[str] | None = None,
        user_query_builder: UserQueryBuilder | None = None,
        read_timeout: float | None = 300.0,
    ) -> None:
        self.agent = agent
        self.db = db if db is not None else getattr(agent, "db", None)
        self.harness_db = harness_db

        if self.harness_db is not None:
            prefix = getattr(self.harness_db, "prefix", None)
            if self.db is not None and prefix:
                apply_table_prefix(self.db, prefix=prefix)
            if hasattr(agent, "db") and agent.db is not None and agent.db is not self.db and prefix:
                apply_table_prefix(agent.db, prefix=prefix)

            if stores is None and hasattr(self.harness_db, "build_stores"):
                stores = self.harness_db.build_stores()
        elif self.db is not None and agno_db_is_persistent(self.db):
            with contextlib.suppress(Exception):
                from ..db.classes import AgnoHarnessDb

                self.harness_db = AgnoHarnessDb.from_agno_db(self.db)
                if stores is None and hasattr(self.harness_db, "build_stores"):
                    stores = self.harness_db.build_stores()

        self.stores = stores or Stores()
        check_history_pairing(self.db, self.stores, allow_ephemeral_agno_db=allow_ephemeral_agno_db)
        self.catalog = catalog
        self.enable_streamui = enable_streamui
        if artifact_root_dir is None and workspace_dir is not None:
            artifact_root_dir = workspace_dir
        self.artifact_root_dir = artifact_root_dir
        self.sequencer_mode = sequencer_mode
        self.record_chunks = record_chunks
        self.trace_dir = trace_dir

        self.user_query_builder = user_query_builder or default_builder(
            timezone=RelayConfig.timezone()
        )
        self.runner = AgentRunner(
            agent,
            enable_subagent_streaming=enable_subagent_streaming,
            user_query_builder=self.user_query_builder,
            read_timeout=read_timeout,
        )
        self.inspectors = InspectorRegistry()
        self.threads = ThreadService(
            self.db,
            stores=self.stores,
            catalog=catalog,
            hidden_tool_names=lambda: self.hidden_tool_names,
        )

        # Registration order is pipeline order, and each position here is a
        # decision. The sub-agent module goes first so it sees every tool call
        # before the filters can hide the one that delegates. The filters go
        # before StreamUI so a hidden tool's output never reaches the fence parser.
        # custom_events only observes, so it is last by convention.
        self.tool_filters = ToolFilterModule()
        self.subagents = SubAgentModule(stores=self.stores, enabled=enable_subagent_streaming)
        self.streamui = StreamUIModule(
            self.catalog,
            enabled=enable_streamui,
            artifact_root_dir=self.artifact_root_dir,
        )
        self.modules = ModuleRegistry(
            [
                self.subagents,
                self.tool_filters,
                self.streamui,
            ]
        )
        self.modules.add(
            CustomEventsModule(
                stores=self.stores,
                is_claimed=lambda name: self.modules.owner_of(name) is not None,
            )
        )

        self._encoder = EventEncoder()
        self._parsers: dict[str, list[EventParser]] = defaultdict(list)
        self._pre_run_hooks: list[PreRunHook] = []
        self._post_run_hooks: list[PostRunHook] = []

        if enable_reasoning_patch:
            self.register_parser(RunEvent.run_content, reasoning_content_parser)

        if enable_compression_streaming:
            self.register_parser(RunEvent.compression_started, compression_events_parser)
            self.register_parser(RunEvent.compression_completed, compression_events_parser)
            self.register_parser("TeamCompressionStarted", compression_events_parser)
            self.register_parser("TeamCompressionCompleted", compression_events_parser)

    @property
    def enable_subagent_streaming(self) -> bool:
        return self.runner.enable_subagent_streaming

    def register_module(self, module: BridgeModule) -> AgentRuntime:
        """Add a capability to the pipeline, after the built-in ones.

        Raises :class:`ModuleConflict` if the name or the custom-event namespace
        is already taken — at assembly time, where a mistake is cheap, rather
        than as two modules quietly fighting over the same frames at run time.
        """
        self.modules.add(module)
        return self

    # ── registration ──────────────────────────────────────────────────────

    def register_parser(self, event_type: Any, parser: EventParser) -> AgentRuntime:
        """Run ``parser`` after the official handler for one chunk category.

        ``event_type`` may be a ``RunEvent`` member or its string value.
        Multiple parsers per category run in registration order.
        """
        key = event_type.value if hasattr(event_type, "value") else str(event_type)
        self._parsers[key].append(parser)
        return self

    def register_tool_filter(self, tool_filter: ToolFilter) -> AgentRuntime:
        """Append a filter to the chain (``None`` drops, an event keeps)."""
        self.tool_filters.add(tool_filter)
        return self

    def on_pre_run(self, hook: PreRunHook) -> AgentRuntime:
        """Register a hook run with the :class:`RunScope` before the agent.

        The hook may mutate the scope — set ``user_id``, seed the shared state
        document, add ``arun`` keyword arguments — perform side effects, and
        yield events. It runs after ``RUN_STARTED`` is on the wire but before
        the agent is started, so a charge it makes or a notice it emits is
        settled before any model output exists, and its events need no special
        handling to be well-formed.
        """
        self._pre_run_hooks.append(hook)
        return self

    def on_post_run(self, hook: PostRunHook) -> AgentRuntime:
        """Register a hook run after the agent, before the run closes.

        It receives the scope and the agent's terminal chunk (``None`` if the
        run failed before producing one) plus the exception, if any. Events it
        yields land inside the run, ahead of ``RUN_FINISHED``, so a summary or a
        receipt is part of the transcript rather than an orphan after it.
        """
        self._post_run_hooks.append(hook)
        return self

    @property
    def hidden_tool_names(self) -> set[str]:
        return self.tool_filters.hidden_tool_names

    # ── live streaming ────────────────────────────────────────────────────

    async def stream(
        self,
        run_input: RunAgentInput,
        *,
        request: Any = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Run once and yield encoded SSE frames."""
        async for event in self.stream_events(
            run_input, request=request, user_id=user_id, metadata=metadata
        ):
            yield self._encoder.encode(event)

    async def stream_events(
        self,
        run_input: RunAgentInput,
        *,
        request: Any = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[BaseEvent]:
        """Run once and yield AG-UI events.

        Lifecycle::

            RUN_STARTED
            pre-run hooks        (scope mutations + side effects + events)
            STATE_SNAPSHOT       (only when there is a state document)
            ...converted chunks, sub-agent chunks, UI blocks...
            post-run hooks       (sees the terminal chunk)
            RUN_FINISHED         (or RUN_ERROR)

        ``RUN_STARTED`` goes out before the hooks even though the hooks run
        first in wall-clock terms: they are "pre-run" with respect to the
        *agent*, not to the wire, and opening the run first is what keeps a
        hook's events from being logged as a protocol repair on every single
        run. The sequencer would buffer them either way, but a repair the
        runtime provokes itself is noise in the audit trail.
        """
        scope = self._make_scope(run_input, user_id, metadata=metadata)
        tracer = RunTracer.open(self.trace_dir, thread_id=scope.thread_id, run_id=scope.run_id)
        translator = EventTranslator(
            scope=scope,
            parsers=self._parsers,
            sequencer_mode=self.sequencer_mode,
            modules=self.modules,
            tracer=tracer,
        )

        error: BaseException | None = None
        disconnected = False
        is_paused = False

        if getattr(self.stores, "threads", None) is not None:
            with contextlib.suppress(Exception):
                prompt = last_user_text(run_input)
                await self.stores.threads.start_turn(
                    thread_id=scope.thread_id,
                    user_id=scope.user_id,
                    run_id=scope.run_id,
                    title=prompt[:40] if prompt else None,
                )

        try:
            async for event in translator.start():
                yield event

            async for event in self._run_pre_hooks(translator, scope, tracer):
                yield event

            # Bound after the hooks so that a hook which switched state sync on
            # is reflected in the snapshot the client receives.
            if tracer is not None:
                tracer.stage("initial_state")
            scope.state_tracker.bind(scope.stream_state)
            for event in scope.state_tracker.initial_events():
                async for out in translator.inject(event):
                    yield out

            resume = detect_resume(run_input)
            if resume is not None:
                for event in resume_result_events(resume):
                    async for out in translator.inject(event):
                        yield out

            async for event in self._pump(translator, scope, run_input, request):
                if (
                    event.type is EventType.CUSTOM
                    and getattr(event, "name", "") == EVENT_RUN_PAUSED
                ):
                    is_paused = True
                    if getattr(self.stores, "threads", None) is not None:
                        with contextlib.suppress(Exception):
                            await self.stores.threads.set_paused(
                                scope.thread_id, is_paused=True, run_id=scope.run_id
                            )
                elif (
                    event.type is EventType.CUSTOM
                    and getattr(event, "name", "") == EVENT_THREAD_TITLE
                ):
                    val = getattr(event, "value", None)
                    if (
                        isinstance(val, dict)
                        and val.get("title")
                        and getattr(self.stores, "threads", None) is not None
                    ):
                        with contextlib.suppress(Exception):
                            await self.stores.threads.set_title(scope.thread_id, val["title"])
                yield event

        except _Disconnected:
            disconnected = True
            scope.disconnected = True
            if tracer is not None:
                tracer.stage("client_disconnected")
        except Exception as exc:  # noqa: BLE001 - any failure becomes RUN_ERROR
            error = exc

        target_db = self.db or getattr(self.agent, "db", None)

        if disconnected:
            if getattr(self.stores, "threads", None) is not None:
                with contextlib.suppress(Exception):
                    await self.stores.threads.set_cancelled(
                        scope.thread_id,
                        reason="Client disconnected / cancelled",
                        run_id=scope.run_id,
                    )
            # No terminal event: the client is gone, and a sequencer flush would
            # only produce frames nobody will read. The modules still get their
            # teardown — a frame nobody reads is pointless, but a module left
            # holding an open bracket, an unwritten record or an unfinished span
            # is a leak, and this is the one exit that used to skip them.
            async for _ in translator.teardown(error=False):
                pass
            translator.record_outcome()
            if target_db is not None:
                with contextlib.suppress(Exception):
                    await seal_session_run(
                        target_db,
                        session_id=scope.thread_id,
                        run_id=scope.run_id,
                        reason="Client disconnected / cancelled",
                        is_error=False,
                        partial_content=translator.accumulated_text or None,
                    )
            return

        async for event in self._run_post_hooks(translator, scope, tracer, error):
            if event.type is EventType.CUSTOM and getattr(event, "name", "") == EVENT_THREAD_TITLE:
                val = getattr(event, "value", None)
                if (
                    isinstance(val, dict)
                    and val.get("title")
                    and getattr(self.stores, "threads", None) is not None
                ):
                    with contextlib.suppress(Exception):
                        await self.stores.threads.set_title(scope.thread_id, val["title"])
            yield event

        if error is not None:
            if getattr(self.stores, "threads", None) is not None:
                with contextlib.suppress(Exception):
                    await self.stores.threads.set_finished(
                        scope.thread_id,
                        is_error=True,
                        error_reason=str(error),
                        run_id=scope.run_id,
                    )
            async for event in translator.fail(error):
                yield event
            for event in translator.close():
                yield event
            if target_db is not None:
                with contextlib.suppress(Exception):
                    await seal_session_run(
                        target_db,
                        session_id=scope.thread_id,
                        run_id=scope.run_id,
                        reason=str(error),
                        is_error=True,
                        partial_content=translator.accumulated_text or None,
                    )
            return

        if not is_paused and getattr(self.stores, "threads", None) is not None:
            with contextlib.suppress(Exception):
                await self.stores.threads.set_finished(
                    scope.thread_id,
                    is_error=False,
                    run_id=scope.run_id,
                )

        async for event in translator.finish():
            yield event

    # ── pipeline steps ────────────────────────────────────────────────────

    def _make_scope(
        self,
        run_input: RunAgentInput,
        user_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> RunScope:
        thread_id = run_input.thread_id or _new_id("thread")
        run_id = run_input.run_id or _new_id("run")
        stream_state = StreamState(thread_id=thread_id, run_id=run_id)
        scope = RunScope(
            thread_id=thread_id,
            run_id=run_id,
            user_id=user_id,
            user_text=last_user_text(run_input.messages),
            stream_state=stream_state,
            state_tracker=StateTracker(validate_state(run_input.state, thread_id)),
            inspector=self.inspectors.open(
                thread_id=thread_id, run_id=run_id, samples_per_type=self.record_chunks
            ),
            forwarded_props=run_input.forwarded_props,
            metadata=dict(metadata or {}),
        )
        if self.artifact_root_dir is not None:
            scope.data["artifact_root_dir"] = self.artifact_root_dir
        return scope

    async def _run_pre_hooks(
        self,
        translator: EventTranslator,
        scope: RunScope,
        tracer: RunTracer | None,
    ) -> AsyncIterator[BaseEvent]:
        for index, hook in enumerate(self._pre_run_hooks):
            if tracer is not None:
                tracer.stage("pre_run_hook", getattr(hook, "__name__", f"hook[{index}]"))
            async for event in hook(scope):
                async for out in translator.inject(event):
                    yield out

    async def _run_post_hooks(
        self,
        translator: EventTranslator,
        scope: RunScope,
        tracer: RunTracer | None,
        error: BaseException | None,
    ) -> AsyncIterator[BaseEvent]:
        for index, hook in enumerate(self._post_run_hooks):
            if tracer is not None:
                tracer.stage("post_run_hook", getattr(hook, "__name__", f"hook[{index}]"))
            async for event in hook(scope, completion=translator.completion_chunk, error=error):
                async for out in translator.inject(event):
                    yield out

    async def _pump(
        self,
        translator: EventTranslator,
        scope: RunScope,
        run_input: RunAgentInput,
        request: Any,
    ) -> AsyncIterator[BaseEvent]:
        if self.harness_db is not None and hasattr(self.harness_db, "ensure_tables"):
            await self.harness_db.ensure_tables()

        async for item in self.runner.run(run_input, scope):
            if request is not None and await _is_disconnected(request):
                raise _Disconnected
            async for event in translator.feed(item):
                yield event

    # ── history ───────────────────────────────────────────────────────────

    async def replay_messages(
        self, thread_id: str, *, user_id: str | None = None
    ) -> list[dict[str, Any]] | None:
        """Rebuild a thread's messages, or ``None`` if it is unknown to this user."""
        return await self.threads.replay_messages(thread_id, user_id=user_id)

    async def get_thread(
        self, thread_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """Get a single thread's metadata and status."""
        return await self.threads.get_thread(thread_id, user_id=user_id)

    async def list_threads(self, *, user_id: str | None = None) -> list[dict[str, Any]]:
        """This user's threads, newest first, with a title and message count."""
        return await self.threads.list_threads(user_id=user_id)

    async def delete_thread(
        self, thread_id: str, *, user_id: str | None = None, hard: bool = False
    ) -> dict[str, Any]:
        """Delete a thread and the records the toolbox added alongside it."""
        return await self.threads.delete_thread(thread_id, user_id=user_id, hard=hard)

    async def generate_thread_title(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        max_turns: int = 1,
        user_text: str | None = None,
        completion_text: str | None = None,
    ) -> str | None:
        """Name a thread with one short completion on ``agent.model``.

        Writes ``session_data["session_name"]`` and returns the title, or
        ``None`` if the model is missing or the call failed. Does not run the
        agent — ``agent.arun()`` would pollute the session.
        """
        title = await _generate_thread_title(
            agent=self.agent,
            db=self.db,
            thread_id=thread_id,
            user_id=user_id,
            max_turns=max_turns,
            user_text=user_text,
            completion_text=completion_text,
        )
        if title and getattr(self.stores, "threads", None) is not None:
            with contextlib.suppress(Exception):
                await self.stores.threads.set_title(thread_id, title)
        return title

    # ── introspection ─────────────────────────────────────────────────────

    def inspect_run(self, run_id: str) -> dict[str, Any] | None:
        """Everything the debug UI knows about one run."""
        inspector = self.inspectors.get(run_id)
        return inspector.as_dict() if inspector is not None else None

    def chunk_samples(self) -> dict[str, Any]:
        """Recorded raw chunk shapes from the latest run, for the Chunks tab."""
        latest = self.inspectors.latest()
        if latest is None:
            return {"samplesPerType": self.record_chunks, "totalChunks": 0, "types": []}
        return latest.recorder.as_dict()

    def chunk_counts(self) -> dict[str, int]:
        latest = self.inspectors.latest()
        return latest.recorder.counts() if latest is not None else {}

    def last_violations(self) -> list[ProtocolViolation]:
        """Protocol repairs from the most recent run (``audit`` mode only)."""
        latest = self.inspectors.latest()
        return list(latest.violations) if latest is not None else []

    def stream_state(self, run_id: str) -> dict[str, Any] | None:
        inspector = self.inspectors.get(run_id)
        return inspector.stream_state if inspector is not None else None

    @property
    def parsers(self) -> dict[str, Sequence[EventParser]]:
        return {key: list(value) for key, value in self._parsers.items()}


class _Disconnected(Exception):
    """The client closed the stream; stop quietly rather than as an error."""


async def _is_disconnected(request: Any) -> bool:
    check = getattr(request, "is_disconnected", None)
    if check is None:
        return False
    try:
        return bool(await check())
    except Exception:  # noqa: BLE001 - a broken check must not kill the run
        return False


__all__ = [
    "EVENT_RUN_CANCELLED",
    "EVENT_RUN_PAUSED",
    "AgentRunFailed",
    "AgentRuntime",
]
