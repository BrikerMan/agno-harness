"""Optional tracing, for anything that speaks OTLP.

Two pieces, and the split is the same one OpenTelemetry itself makes: a library
instruments, an application configures.

:class:`~agno_harness.observability.module.ObservabilityModule` is the
instrumentation — one span per AG-UI run, with the agent's own spans nested
inside it. :func:`~agno_harness.observability.setup.setup_otlp` is the
configuration, and is a convenience rather than a requirement; an app that
already installs a ``TracerProvider`` should skip it.

Importing this package requires ``opentelemetry-api``, which is why the
package never imports it eagerly. Install it with ``agno-harness[otel]``.
"""

from .module import ObservabilityModule, turn_run_span_name
from .setup import patch_context_detach, setup_otlp

patch_context_detach()

__all__ = [
    "ObservabilityModule",
    "patch_context_detach",
    "setup_otlp",
    "turn_run_span_name",
]
