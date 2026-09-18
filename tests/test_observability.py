"""Tracing: what ends up in the backend, and what must never end up nested.

The interesting assertions here are about the span *tree* rather than about any
one span. A trace whose spans are all present but wrongly parented is worse than
no trace: it reads as though a sub-agent answered the parent's question, or —
the case that gave this module its shape — as though one user's run happened
inside another's.

Every test drives a real ``AgentRuntime`` against a scripted agent and reads the
spans back out of an in-memory exporter, so what is asserted is what a collector
would have received.
"""

from __future__ import annotations

import asyncio
import base64
import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracerProvider, StatusCode

from agno_harness import AgentRuntime, ObservabilityModule, SequencerMode, setup_otlp
from agno_harness.core.streamui import BlockSchema, CardCatalog

# Not ``as setup_module``: that is a pytest xunit hook name, and pytest would
# try to call the module.
from agno_harness.observability import setup as otlp_setup

from .conftest import (
    FakeAgent,
    collect,
    content,
    make_input,
    run_completed,
    tool_completed,
    tool_started,
)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter: InMemorySpanExporter) -> TracerProvider:
    """A provider of our own, never the global one.

    OpenTelemetry refuses to replace an installed provider, so a test that sets
    the global one poisons every test after it and cannot itself be run twice.
    """
    provider = TracerProvider()
    # Simple rather than batch: a test should not have to flush and wait.
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def build(provider, chunks, *, name="demo", raise_at=None, **kwargs) -> AgentRuntime:
    agent = FakeAgent(chunks, raise_at=raise_at)
    agent.name = name
    runtime = AgentRuntime(agent=agent, sequencer_mode=SequencerMode.REPAIR)
    runtime.register_module(
        ObservabilityModule(tracer_provider=provider, **kwargs).bind_agent(agent)
    )
    return runtime


class _Notes(BlockSchema):
    schema_name = "notes"
    body = "text"


def spans_by_name(exporter: InMemorySpanExporter) -> dict[str, object]:
    return {span.name: span for span in exporter.get_finished_spans()}


class TestTheRunSpan:
    async def test_a_run_produces_one_span_named_after_the_agent(self, provider, exporter):
        runtime = build(provider, [content("hi"), run_completed()])
        await collect(runtime.stream_events(make_input()))

        finished = exporter.get_finished_spans()
        assert [span.name for span in finished] == ["demo.turn-run"]

    async def test_the_span_carries_the_identity_a_backend_groups_by(self, provider, exporter):
        runtime = build(provider, [content("hi"), run_completed()])
        await collect(runtime.stream_events(make_input(thread_id="t-7"), user_id="alice"))

        attributes = exporter.get_finished_spans()[0].attributes
        assert attributes["session.id"] == "t-7"
        assert attributes["user.id"] == "alice"

    async def test_the_span_carries_the_user_facing_turn(self, provider, exporter):
        """Langfuse maps a trace's input/output from the root span.

        AgnoInstrumentor puts the constructed prompt on a *child* span, so a
        backend that reads the root would otherwise show an empty turn. The
        values here are what the user said and what they saw, not the prompt.
        """
        runtime = build(provider, [content("hi"), content(" there"), run_completed()])
        await collect(runtime.stream_events(make_input("what is 2+2?")))

        attributes = exporter.get_finished_spans()[0].attributes
        assert attributes["input.value"] == "what is 2+2?"
        assert attributes["output.value"] == "hi there"

    async def test_a_run_with_no_user_text_omits_input(self, provider, exporter):
        await collect(build(provider, [run_completed()]).stream_events(make_input(messages=[])))

        attributes = exporter.get_finished_spans()[0].attributes
        assert "input.value" not in attributes
        assert "output.value" not in attributes

    async def test_it_counts_what_the_client_received(self, provider, exporter):
        chunks = [
            content("thinking"),
            tool_started("c1", "search", {"q": "x"}),
            tool_completed("c1", "search", "found"),
            content("done"),
            run_completed(),
        ]
        await collect(build(provider, chunks).stream_events(make_input()))

        attributes = exporter.get_finished_spans()[0].attributes
        assert attributes["agui.tool_calls"] == 1
        assert attributes["agui.frames"] > 0
        assert attributes["agui.ttft_ms"] >= 0
        assert attributes["agui.disconnected"] is False

    async def test_it_counts_the_cards_the_agent_streamed(self, provider, exporter):
        card = '```stream-ui {"schema": "notes"}\n{"text": "hello"}\n```\n'
        agent = FakeAgent([content(card), run_completed()])
        agent.name = "demo"
        runtime = AgentRuntime(
            agent=agent,
            catalog=CardCatalog([_Notes]),
            # A card-only answer leaves an empty text message, which STRICT
            # treats as a violation and REPAIR quietly drops.
            sequencer_mode=SequencerMode.REPAIR,
        )
        runtime.register_module(ObservabilityModule(tracer_provider=provider).bind_agent(agent))
        await collect(runtime.stream_events(make_input()))

        assert exporter.get_finished_spans()[0].attributes["agui.cards"] == 1

    async def test_a_run_with_no_answer_reports_no_time_to_first_token(self, provider, exporter):
        """Absent rather than zero: nothing was written, so there is no latency."""
        await collect(build(provider, [run_completed()]).stream_events(make_input()))

        assert "agui.ttft_ms" not in exporter.get_finished_spans()[0].attributes

    async def test_a_failed_run_is_marked_failed(self, provider, exporter):
        runtime = build(provider, [content("half an answer")], raise_at=1)
        await collect(runtime.stream_events(make_input()))

        span = exporter.get_finished_spans()[0]
        assert span.status.status_code is StatusCode.ERROR

    async def test_a_disconnected_client_still_closes_the_span(self, provider, exporter):
        """The exit with no terminal event is the one that used to leak.

        A span that is never ended is never exported, so the run would be
        missing from the trace entirely rather than merely incomplete.
        """

        class Disconnected:
            async def is_disconnected(self):
                return True

        runtime = build(provider, [content("never sent"), run_completed()])
        await collect(runtime.stream_events(make_input(), request=Disconnected()))

        span = exporter.get_finished_spans()[0]
        assert span.attributes["agui.disconnected"] is True


class TestNesting:
    async def test_the_agents_own_spans_hang_inside_the_run(self, provider, exporter):
        """The whole point of the module.

        The fake agent opens a span while it streams, standing in for what
        ``AgnoInstrumentor`` does. If the run's span is not current at that
        moment, this span becomes a second root and the backend shows two
        unrelated traces for one answer.
        """
        tracer = provider.get_tracer("fake-agno")

        class InstrumentedAgent(FakeAgent):
            async def _stream(self):
                with tracer.start_as_current_span("Agent.run"):
                    async for chunk in super()._stream():
                        yield chunk

        agent = InstrumentedAgent([content("hi"), content(" there"), run_completed()])
        agent.name = "demo"
        runtime = AgentRuntime(agent=agent)
        runtime.register_module(ObservabilityModule(tracer_provider=provider).bind_agent(agent))
        await collect(runtime.stream_events(make_input()))

        spans = spans_by_name(exporter)
        assert set(spans) == {"demo.turn-run", "Agent.run"}
        assert spans["Agent.run"].parent.span_id == spans["demo.turn-run"].context.span_id
        assert spans["Agent.run"].context.trace_id == spans["demo.turn-run"].context.trace_id

    async def test_two_runs_at_once_do_not_nest_inside_each_other(self, provider, exporter):
        """Two roots, two traces, no leakage between them.

        This is what the per-step context exists for. Held across a ``yield``
        instead, the second run would open its span inside the first, and one
        customer's trace would contain another's conversation.
        """
        slow = build(provider, [content("a"), content("b"), run_completed()], name="slow")
        fast = build(provider, [content("c"), run_completed()], name="fast")

        await asyncio.gather(
            collect(slow.stream_events(make_input(thread_id="t1", run_id="r1"))),
            collect(fast.stream_events(make_input(thread_id="t2", run_id="r2"))),
        )

        spans = spans_by_name(exporter)
        slow_span = spans["slow.turn-run"]
        fast_span = spans["fast.turn-run"]
        assert slow_span.parent is None
        assert fast_span.parent is None
        assert slow_span.context.trace_id != fast_span.context.trace_id

    async def test_the_context_does_not_leak_to_whoever_reads_the_stream(self, provider, caplog):
        """A consumer iterating a run must not find itself inside its span."""
        caplog.set_level(logging.ERROR, logger="opentelemetry.context")
        runtime = build(provider, [content("hi"), content(" there"), run_completed()])

        async for _ in runtime.stream_events(make_input()):
            assert trace.get_current_span() is trace.INVALID_SPAN
        assert "Failed to detach context" not in caplog.text

    async def test_a_span_held_across_the_agents_yield_does_not_leak_either(self, provider, caplog):
        """AgnoInstrumentor keeps the agent span attached across ``yield``.

        After each chunk the consumer must still be outside the run span,
        and no OpenTelemetry context detach error should be raised.
        """
        caplog.set_level(logging.ERROR, logger="opentelemetry.context")
        tracer = provider.get_tracer("fake-agno")

        class InstrumentedAgent(FakeAgent):
            async def _stream(self):
                with tracer.start_as_current_span("Agent.run"):
                    async for chunk in super()._stream():
                        yield chunk

        agent = InstrumentedAgent([content("a"), content("b"), run_completed()])
        agent.name = "demo"
        runtime = AgentRuntime(agent=agent)
        runtime.register_module(ObservabilityModule(tracer_provider=provider).bind_agent(agent))

        async for _ in runtime.stream_events(make_input()):
            assert trace.get_current_span() is trace.INVALID_SPAN
        assert "Failed to detach context" not in caplog.text


class TestNaming:
    async def test_an_unnamed_agent_falls_back_to_a_fixed_name(self, provider, exporter):
        agent = FakeAgent([run_completed()])
        runtime = AgentRuntime(agent=agent)
        runtime.register_module(ObservabilityModule(tracer_provider=provider).bind_agent(agent))
        await collect(runtime.stream_events(make_input()))

        assert exporter.get_finished_spans()[0].name == "agent.turn-run"

    async def test_the_name_can_be_computed_per_run(self, provider, exporter):
        """For services routing several conversation kinds through one agent."""
        runtime = build(
            provider,
            [run_completed()],
            span_name=lambda scope: "chat-group" if scope.thread_id.startswith("g-") else "chat-dm",
        )
        await collect(runtime.stream_events(make_input(thread_id="g-1")))

        assert exporter.get_finished_spans()[0].name == "chat-group"

    def test_turn_run_span_name_slugs_and_does_not_double_suffix(self):
        from agno_harness.observability.module import turn_run_span_name

        assert turn_run_span_name("movie-assistant") == "movie-assistant.turn-run"
        assert turn_run_span_name("Movie Bot") == "movie-bot.turn-run"
        assert turn_run_span_name(None) == "agent.turn-run"
        assert turn_run_span_name("movie-assistant.turn-run") == "movie-assistant.turn-run"


class TestSetup:
    """``setup_otlp`` reads the environment, and refuses to be dramatic about it.

    Every case here keeps its hands off the global ``TracerProvider``: one
    installed by a test would collect spans for the rest of the session, and
    OpenTelemetry will not let a later test replace it.
    """

    @pytest.fixture
    def captured(self, monkeypatch) -> dict[str, object]:
        captured: dict[str, object] = {}

        class FakeExporter:
            def __init__(self, endpoint, headers):
                captured["endpoint"] = endpoint
                captured["headers"] = headers

            def shutdown(self) -> None: ...

        monkeypatch.setattr(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            FakeExporter,
        )
        monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", lambda provider: None)
        monkeypatch.setattr(otlp_setup, "_instrument_agno", lambda: None)
        return captured

    def test_nothing_configured_installs_nothing(self, monkeypatch):
        """The state of every developer's laptop, and it must not be an error."""
        for key in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_HEADERS",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
            "LANGFUSE_BASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)

        assert setup_otlp() is None

    def test_the_documented_environment_variables_are_the_ones_read(self, monkeypatch, captured):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-key=abc,x-other=1")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "svc")
        monkeypatch.setenv("OTEL_ENVIRONMENT", "staging")

        provider = setup_otlp(force=True)

        assert captured["endpoint"] == "http://collector:4318/v1/traces"
        assert captured["headers"] == {"x-key": "abc", "x-other": "1"}
        attributes = provider.resource.attributes
        assert attributes["service.name"] == "svc"
        # Both spellings: the convention was renamed and backends disagree.
        assert attributes["deployment.environment.name"] == "staging"
        assert attributes["deployment.environment"] == "staging"

    def test_langfuse_keys_derive_the_endpoint_and_the_header(self, captured):
        """The vendor shortcut, and the only place a vendor is named."""
        setup_otlp(
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
            langfuse_base_url="https://cloud.langfuse.com/",
            force=True,
        )

        assert captured["endpoint"] == "https://cloud.langfuse.com/api/public/otel/v1/traces"
        expected = base64.b64encode(b"pk:sk").decode()
        assert captured["headers"]["Authorization"] == f"Basic {expected}"

    def test_an_endpoint_already_naming_the_signal_is_left_alone(self, captured):
        setup_otlp(endpoint="http://collector:4318/v1/traces", force=True)

        assert captured["endpoint"] == "http://collector:4318/v1/traces"

    def test_a_broken_instrumentor_does_not_stop_the_process(self, monkeypatch, caplog):
        """Telemetry failing to install must never be able to fail a boot.

        The version of Agno and the version of the instrumentation for it are
        released by different people, so this is a matter of when.
        """

        class Exploding:
            def instrument(self) -> None:
                raise RuntimeError("incompatible agno version")

        monkeypatch.setattr(
            "openinference.instrumentation.agno.AgnoInstrumentor", Exploding, raising=False
        )

        otlp_setup._instrument_agno()

        assert "AgnoInstrumentor failed to install" in caplog.text


class TestWithoutABackend:
    async def test_nothing_configured_means_no_spans_and_no_errors(self, exporter):
        """The default state of any app that has not set up a collector."""
        runtime = build(NoOpTracerProvider(), [content("hi"), run_completed()])
        events = await collect(runtime.stream_events(make_input()))

        assert [e.type.value for e in events][-1] == "RUN_FINISHED"
        assert exporter.get_finished_spans() == ()
