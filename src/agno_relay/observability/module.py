"""ObservabilityModule — one span per run, and everything else nests inside it.

WHAT THIS DOES NOT DO
---------------------
It does not trace the model. ``openinference-instrumentation-agno`` already
emits an ``Agent.run`` span carrying the constructed prompt and the model
response, an LLM span per model call carrying the token counts a backend turns
into cost, and a span per tool carrying its arguments and result. Duplicating
any of *that* here would put two of everything in front of whoever reads the
trace.

So this module adds the three things instrumentation cannot see.

The first is *grouping*. Left alone, the agent's spans are parented by whatever
context happens to be current when Agno runs, and the work around the agent —
pre-run hooks, fence parsing, the sequencer, a detached run's whole life after
the request ended — is in no trace at all. One span per AG-UI run, made current
while the agent runs, turns that into a single trace: the run at the root, the
agent under it, the model and the tools under that, and a sub-agent nested where
it was delegated.

The second is the *user-facing turn*. Langfuse (and anything else that maps a
trace's input/output from the root span) never sees the child's ``Agent.run``
I/O in the session list. The constructed prompt is also the wrong thing to
show there. This span therefore carries OpenInference ``input.value`` (the
latest user message) and ``output.value`` (the text the client actually
received). Those are not a second copy of the model I/O; they are a different
question.

The third is the *delivery layer*: time to first token, how many frames the
client got, whether it was still there at the end. Those are facts about the
stream rather than about the model, and nothing else is watching the stream.

WHY THE SPAN IS NOT `start_as_current_span`
-------------------------------------------
Because a run is an async generator, and Python has no per-generator context
(PEP 568 was never implemented). Entering a context manager inside a generator
leaks it into whoever consumes the generator, so with two runs interleaving on
one event loop the second would open its span inside the first, and a trace
would end up containing another user's conversation.

The span is therefore started detached and held in the run's scratch space, and
made current only around each wait for the agent's next chunk — a single
``await``, inside the task that owns the run, via ``RunScope.step_context``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from ag_ui.core import BaseEvent, EventType
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, StatusCode

from ..core.streamui import EVENT_BLOCK_START
from ..runtime.module import Module
from ..runtime.modules.subagent import EVENT_SUBAGENT_START
from ..runtime.scope import RunScope
from .setup import patch_context_detach

TRACER_NAME = "agno_relay"

#: Read by Langfuse, Phoenix and anything else that speaks OpenInference. These
#: are also plain OpenTelemetry / OpenInference semantic conventions, which is
#: the point: no vendor's name appears anywhere in this file.
SESSION_ID = "session.id"
USER_ID = "user.id"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"

#: The delivery-layer facts, namespaced under ``agui`` because they are this
#: project's to define and nobody else's.
TTFT_MS = "agui.ttft_ms"
DURATION_MS = "agui.duration_ms"
FRAMES = "agui.frames"
TOOL_CALLS = "agui.tool_calls"
CARDS = "agui.cards"
SUBAGENTS = "agui.subagents"
DISCONNECTED = "agui.disconnected"
RUN_ID = "agui.run_id"
THREAD_ID = "agui.thread_id"

SpanNamer = Callable[[RunScope], str]


class ObservabilityModule(Module):
    """Opens one span per run and hangs the agent's own spans inside it.

    Parameters
    ----------
    agent_name:
        The span name, and therefore the trace name in most backends. Defaults
        to ``"agui.run"``; :meth:`bind_agent` takes it from an agent instead.

        Keep it low-cardinality. A backend groups by this string, so a name with
        a run id in it produces one bucket per run, which is the same as no
        grouping at all. Anything identifying belongs in an attribute.
    span_name:
        A function from the run to its name, for callers who route several kinds
        of conversation through one agent and want them separated — ``chat-dm``
        against ``chat-group``, say. Overrides ``agent_name``, and the
        low-cardinality rule applies here rather harder.
    tracer_provider:
        Defaults to the global one. Passing it explicitly is mostly for tests,
        which need a provider that does not leak between cases.

    Notes
    -----
    With no ``TracerProvider`` configured, the OpenTelemetry API hands back a
    no-op tracer and everything here becomes a few attribute reads. Leaving the
    module registered in an environment with no collector costs nothing, which
    is what makes it safe to wire in once rather than conditionally.
    """

    name = "observability"
    # No CUSTOM events of its own: this module watches, and never speaks.
    namespace = None

    def __init__(
        self,
        *,
        agent_name: str | None = None,
        span_name: SpanNamer | None = None,
        tracer_provider: Any = None,
    ) -> None:
        patch_context_detach()
        self._agent_name = agent_name
        self._span_name = span_name
        self._tracer = trace.get_tracer(TRACER_NAME, tracer_provider=tracer_provider)

    def bind_agent(self, agent: Any) -> ObservabilityModule:
        """Take the default span name from an agent, unless one was given.

        Worth doing, and worth naming your agents for: a service with a router
        agent and three specialists produces three traces called ``agui.run``
        otherwise, and telling them apart means opening each one.
        """
        if self._agent_name is None:
            self._agent_name = getattr(agent, "name", None)
        return self

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def on_run_start(self, run: RunScope) -> AsyncIterator[BaseEvent]:
        attributes: dict[str, Any] = {
            SESSION_ID: run.thread_id,
            USER_ID: run.user_id or "",
            THREAD_ID: run.thread_id,
            RUN_ID: run.run_id,
        }
        if run.user_text:
            attributes[INPUT_VALUE] = run.user_text
        span = self._tracer.start_span(
            self._name_for(run),
            kind=SpanKind.SERVER,
            attributes=attributes,
        )
        state = self.data(run)
        state["span"] = span
        state["started"] = time.perf_counter()
        state["counts"] = dict.fromkeys((FRAMES, TOOL_CALLS, CARDS, SUBAGENTS), 0)
        state["text"] = []
        run.step_context = _make_step_context(span)
        return
        yield  # pragma: no cover - makes this an async generator

    async def observe(self, event: BaseEvent, run: RunScope) -> None:
        """Count what the client actually received.

        Reading the sequencer's output rather than the raw stream is deliberate:
        a frame that was repaired away never reached anybody, and a time to
        first token measured against a frame nobody saw is a lie.
        """
        state = self.data(run)
        counts = state.get("counts")
        if counts is None:
            return
        counts[FRAMES] += 1

        etype = getattr(event, "type", None)
        if etype is EventType.TEXT_MESSAGE_CONTENT:
            delta = getattr(event, "delta", None) or ""
            if delta:
                state.setdefault("text", []).append(delta)
            if "ttft" not in state:
                state["ttft"] = time.perf_counter() - state["started"]
                _add_event(state.get("span"), "agui.first_token")
        elif etype is EventType.TOOL_CALL_START:
            counts[TOOL_CALLS] += 1
        elif etype is EventType.CUSTOM:
            name = getattr(event, "name", "")
            if name == EVENT_BLOCK_START:
                counts[CARDS] += 1
            elif name == EVENT_SUBAGENT_START:
                counts[SUBAGENTS] += 1

    async def on_run_finish(self, run: RunScope, *, error: bool) -> AsyncIterator[BaseEvent]:
        state = self.data(run)
        span: Span | None = state.pop("span", None)
        run.step_context = None
        if span is not None:
            self._close(span, run, state, error=error)
        return
        yield  # pragma: no cover - makes this an async generator

    def _close(self, span: Span, run: RunScope, state: dict[str, Any], *, error: bool) -> None:
        span.set_attribute(DURATION_MS, _ms(time.perf_counter() - state["started"]))
        span.set_attribute(DISCONNECTED, run.disconnected)
        if "ttft" in state:
            span.set_attribute(TTFT_MS, _ms(state["ttft"]))
        for key, value in state.get("counts", {}).items():
            span.set_attribute(key, value)
        text = "".join(state.get("text") or [])
        if text:
            span.set_attribute(OUTPUT_VALUE, text)

        # The exception itself already reaches the client as a RUN_ERROR frame.
        # What a backend needs is the status, so that one failed run is findable
        # among a thousand successful ones.
        span.set_status(StatusCode.ERROR if error else StatusCode.OK)
        span.end()

    # ── naming ────────────────────────────────────────────────────────────

    def _name_for(self, run: RunScope) -> str:
        if self._span_name is not None:
            return self._span_name(run)
        return self._agent_name or "agui.run"


def _make_step_context(span: Span) -> Callable[[], AbstractContextManager[Any]]:
    """A factory for "make this span current", entered once per agent step."""
    context = trace.set_span_in_context(span)

    @contextmanager
    def enter() -> Iterator[None]:
        # ``attach`` is ContextVar.set: it replaces the current context. Do
        # not ``detach`` the token. AgnoInstrumentor attaches the agent span
        # once and holds it across ``yield``; resetting *our* token then
        # raises ``ValueError`` (different ``contextvars.Context``), and the
        # public ``detach()`` logs that as ``Failed to detach context`` on
        # every chunk. Setting the previous context back is the restore.
        previous = otel_context.get_current()
        otel_context.attach(context)
        try:
            yield
        finally:
            otel_context.attach(previous)

    return enter


def _add_event(span: Span | None, name: str) -> None:
    if span is not None:
        span.add_event(name)


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


__all__ = ["ObservabilityModule"]
