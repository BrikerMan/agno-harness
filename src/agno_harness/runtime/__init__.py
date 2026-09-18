"""runtime — everything that knows about Agno.

Three collaborators, each usable on its own:

:class:`~agno_harness.runtime.translator.EventTranslator`
    Agno chunks in, AG-UI events out. No agent, no HTTP.
:class:`~agno_harness.runtime.runner.AgentRunner`
    Starts or resumes ``agent.arun()`` and yields what comes out.
:class:`~agno_harness.runtime.runtime.AgentRuntime`
    Assembles the two, plus hooks, stores and history. The batteries-included
    entry point.

This layer may import Agno freely; it must not import FastAPI, so that a run can
be driven from a script, a worker or a test without a web framework in the
picture.
"""

from .closure import (
    attach_checkpoint_to_session,
    close_dangling_tool_calls,
    install_sealed_history_hook,
    sanitize_reason,
    seal_session_run,
    strip_stream_ui,
)
from .compression import (
    SmartCompressionManager,
    make_checkpoint_hook,
    structural_prune,
)
from .hitl import ResumeRequest, describe_pause, detect_resume, encode_tool_result
from .inspector import InspectorRegistry, RunInspector
from .longrun import LongRunError, LongRunManager, RunNotOwned
from .module import BridgeModule, Module, ModuleConflict, ModuleRegistry
from .modules.streamui import StreamUIModule, UIItem, UISignal, emit_item, emit_text, ui_block
from .modules.subagent import (
    EVENT_SUBAGENT_END,
    EVENT_SUBAGENT_START,
    SubAgentBus,
    SubStream,
    substream,
)
from .parsers import (
    compression_events_parser,
    reasoning_content_parser,
    subagent_steps_parser,
)
from .recorder import ChunkRecorder, ChunkSample
from .replay import input_to_text, run_to_messages, session_to_messages
from .runner import AgentRunner
from .runtime import AgentRuntime
from .scope import RunScope
from .state import StateTracker
from .storage_guard import StoragePairingError
from .threads import ThreadService
from .titles import (
    EVENT_THREAD_TITLE,
    make_thread_title_hook,
    make_thread_title_pre_hook,
    setup_thread_titles,
)
from .tracing import TRACE_DIR_ENV, RunTracer
from .translator import (
    EVENT_RUN_CANCELLED,
    EVENT_RUN_PAUSED,
    AgentRunFailed,
    EventTranslator,
    make_run_scope,
)
from .types import EventParser, PostRunHook, PreRunHook

__all__ = [
    "EVENT_RUN_CANCELLED",
    "EVENT_RUN_PAUSED",
    "EVENT_SUBAGENT_END",
    "EVENT_SUBAGENT_START",
    "EVENT_THREAD_TITLE",
    "TRACE_DIR_ENV",
    "AgentRunFailed",
    "AgentRunner",
    "AgentRuntime",
    "BridgeModule",
    "ChunkRecorder",
    "ChunkSample",
    "EventParser",
    "EventTranslator",
    "InspectorRegistry",
    "LongRunError",
    "LongRunManager",
    "Module",
    "ModuleConflict",
    "ModuleRegistry",
    "PostRunHook",
    "PreRunHook",
    "ResumeRequest",
    "RunInspector",
    "RunNotOwned",
    "RunScope",
    "RunTracer",
    "StateTracker",
    "StoragePairingError",
    "StreamUIModule",
    "SubAgentBus",
    "UIItem",
    "UISignal",
    "SubStream",
    "ThreadService",
    "describe_pause",
    "detect_resume",
    "encode_tool_result",
    "input_to_text",
    "make_run_scope",
    "make_thread_title_hook",
    "make_thread_title_pre_hook",
    "make_checkpoint_hook",
    "reasoning_content_parser",
    "run_to_messages",
    "session_to_messages",
    "setup_thread_titles",
    "emit_item",
    "emit_text",
    "subagent_steps_parser",
    "substream",
    "ui_block",
    "attach_checkpoint_to_session",
    "close_dangling_tool_calls",
    "compression_events_parser",
    "install_sealed_history_hook",
    "sanitize_reason",
    "seal_session_run",
    "strip_stream_ui",
    "SmartCompressionManager",
    "structural_prune",
]
