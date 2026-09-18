"""setup_otlp — turn on tracing from the environment, or do nothing.

Every deployment needs the same twenty lines: build a resource, add a batch
processor pointed at a collector, install the agent instrumentation. They are
here so that a service does not have to carry its own copy, and so the two
things that are easy to get wrong are got right once.

The first is the no-op. Called with nothing configured, this logs a line and
returns; it does not raise, and it does not install a provider. A developer
running the app locally is not required to have a collector, and forgetting to
set the variables in one environment degrades to "no traces" rather than to "no
service".

The second is the batch processor. Exporting a span inline blocks whatever
produced it, and what produces spans here is a streaming run — so the simple
processor turns a slow collector into a stuttering answer.

Nothing in this module is specific to a vendor. Langfuse is reachable because it
speaks OTLP, and the convenience arguments below only assemble the endpoint and
the basic-auth header that its documentation gives you. Point the standard
``OTEL_EXPORTER_OTLP_*`` variables somewhere else and everything upstream of
here is unchanged.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME = "agui-agent"


def setup_otlp(
    *,
    endpoint: str | None = None,
    headers: str | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    langfuse_public_key: str | None = None,
    langfuse_secret_key: str | None = None,
    langfuse_base_url: str | None = None,
    instrument_agno: bool = True,
    force: bool = False,
) -> Any:
    """Install an OTLP tracer provider, or return ``None`` if unconfigured.

    Every argument falls back to an environment variable, so the usual call is
    ``setup_otlp()`` at startup and the configuration lives with the deployment:

    ``OTEL_EXPORTER_OTLP_ENDPOINT``  where to send spans
    ``OTEL_EXPORTER_OTLP_HEADERS``   auth, as ``key=value,key=value``
    ``OTEL_SERVICE_NAME``            what to call this service
    ``OTEL_ENVIRONMENT``             ``prod``, ``staging``, a developer's name
    ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_BASE_URL``
        a shortcut for the above two: given these, the endpoint and the
        basic-auth header are derived. Nothing else in the toolbox knows this
        vendor exists, and the derivation is four lines you can read below.

    ``instrument_agno`` installs ``openinference-instrumentation-agno``, which
    is what produces the model, token and tool spans. It is best-effort: a
    missing or incompatible package logs and is skipped, because an
    observability dependency must not be able to stop a service from starting.

    Returns the ``TracerProvider`` it installed, or ``None`` when it did
    nothing.
    """
    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or ""
    headers = headers or os.getenv("OTEL_EXPORTER_OTLP_HEADERS") or ""

    public_key = langfuse_public_key or os.getenv("LANGFUSE_PUBLIC_KEY") or ""
    secret_key = langfuse_secret_key or os.getenv("LANGFUSE_SECRET_KEY") or ""
    base_url = langfuse_base_url or os.getenv("LANGFUSE_BASE_URL") or ""
    if public_key and secret_key and base_url and not endpoint:
        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        endpoint = f"{base_url.rstrip('/')}/api/public/otel"
        headers = headers or f"Authorization=Basic {auth}"

    if not endpoint:
        logger.info(
            "Tracing is off: no OTEL_EXPORTER_OTLP_ENDPOINT and no Langfuse keys. "
            "Runs will stream normally and produce no spans."
        )
        return None

    patch_context_detach()

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanLimits, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    existing = trace.get_tracer_provider()
    if not force and isinstance(existing, TracerProvider):
        # Installing a second provider silently orphans everything already
        # instrumented against the first, which is a bad afternoon to debug.
        logger.warning("A TracerProvider is already installed; leaving it alone.")
        return existing

    env = environment or os.getenv("OTEL_ENVIRONMENT") or "default"
    # Allow up to 1024 attributes per span so long multi-turn sessions with dozens
    # of tool calls don't evict the earliest attributes (input.value, system prompt).
    span_limits = SpanLimits(max_attributes=1024)
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name
                or os.getenv("OTEL_SERVICE_NAME")
                or DEFAULT_SERVICE_NAME,
                # Current semantic conventions renamed this attribute and not
                # every backend has followed; both are cheap.
                "deployment.environment.name": env,
                "deployment.environment": env,
            }
        ),
        span_limits=span_limits,
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=_traces_url(endpoint), headers=_headers(headers))
        )
    )
    trace.set_tracer_provider(provider)

    if instrument_agno:
        _instrument_agno()

    logger.info("Tracing on: exporting spans to %s (environment=%s)", endpoint, env)
    return provider


def _traces_url(endpoint: str) -> str:
    """The traces signal's URL, whichever form the endpoint was given in."""
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith("/v1/traces") else f"{endpoint}/v1/traces"


def _headers(raw: str) -> dict[str, str]:
    """Parse ``key=value,key=value``, the form the OTEL_ variables use."""
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {key.strip(): value.strip() for key, value in pairs}


def _instrument_agno() -> None:
    try:
        from openinference.instrumentation.agno import AgnoInstrumentor
    except ImportError:
        logger.warning(
            "openinference-instrumentation-agno is not installed: runs will be traced, "
            "but without the model, token and tool spans it contributes. "
            "Install agno-harness[otel] to get it."
        )
        return
    try:
        AgnoInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001 - never let telemetry stop a boot
        logger.warning("AgnoInstrumentor failed to install (%s); continuing without it.", exc)


def patch_context_detach() -> None:
    """Demote/suppress OpenTelemetry's benign "Failed to detach context" errors.

    In asynchronous streaming generators (PEP 525), contextvars across yield/resume
    can bridge event-loop tasks or context snapshots. Agno and third-party instrumentations
    (e.g. openinference-instrumentation-agno) wrap generator yields in span contexts,
    causing context.detach(token) to encounter ValueError("was created in a different Context")
    at generator cleanup/completion.

    This matches the known issue and solution in MLflow (PR #13914), Langfuse, and Google ADK.
    The trace spans are already finished and exported; the reset failure is harmless.
    """
    try:
        from opentelemetry.context.contextvars_context import ContextVarsRuntimeContext

        if not getattr(ContextVarsRuntimeContext, "_better_agno_safe_detach", False):

            def safe_detach(self: Any, token: object) -> None:
                with contextlib.suppress(ValueError):
                    self._current_context.reset(token)

            ContextVarsRuntimeContext.detach = safe_detach  # type: ignore[method-assign]
            ContextVarsRuntimeContext._better_agno_safe_detach = True  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        from opentelemetry.context import logger as otel_logger

        class _LogDemotionFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                if (
                    record.name == "opentelemetry.context"
                    and "Failed to detach context" in record.getMessage()
                ):
                    record.levelno = logging.DEBUG
                    record.levelname = "DEBUG"
                    return otel_logger.isEnabledFor(logging.DEBUG)
                return True

        if not any(isinstance(f, _LogDemotionFilter) for f in otel_logger.filters):
            otel_logger.addFilter(_LogDemotionFilter())
    except Exception:
        pass


__all__ = ["patch_context_detach", "setup_otlp"]
