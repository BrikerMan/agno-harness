"""The layer boundaries, enforced.

A directory named ``core`` stays free of Agno only for as long as somebody
remembers that it should. These tests do the remembering: they read the import
statements of every module in the package and fail on the first one that reaches
across a boundary it is not allowed to cross.

Two rules, for two different reasons:

``core`` may not import ``agno``
    so the protocol layer can be exercised against hand-written event sequences,
    and so a future non-Agno runtime could reuse it unchanged.

``core``, ``runtime`` and ``stores`` may not import ``fastapi``
    so a caller can drive a run from a script, a worker or a test without a web
    framework in the picture. This is the "full manual" path the README
    documents, and a stray import would break the layering silently — the code
    would still work for everyone who happens to have FastAPI installed.

``observability`` is the only layer that may import ``opentelemetry``
    for the same reason: it lives behind an extra, so an eager import anywhere
    else would turn an optional dependency into an ``ImportError`` on ``import
    agno_relay``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "agno_relay"

# layer -> package names it must not reach for.
FORBIDDEN: dict[str, tuple[str, ...]] = {
    "core": ("agno", "fastapi", "starlette", "opentelemetry"),
    "runtime": ("fastapi", "starlette", "opentelemetry"),
    "stores": ("fastapi", "starlette", "opentelemetry"),
    "transport": ("opentelemetry",),
}


def _imported_roots(path: Path) -> set[str]:
    """Top-level package names imported by one module, absolute imports only."""
    tree = ast.parse(path.read_text(), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _modules(layer: str) -> list[Path]:
    return sorted((PACKAGE_ROOT / layer).rglob("*.py"))


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_does_not_import_forbidden_packages(layer: str) -> None:
    modules = _modules(layer)
    assert modules, f"no modules found under {layer}/ — did the layout change?"

    offenders = [
        f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(bad)}"
        for path in modules
        if (bad := _imported_roots(path) & set(FORBIDDEN[layer]))
    ]
    assert not offenders, "\n".join(offenders)


def test_transport_and_channels_are_the_only_layers_that_import_fastapi() -> None:
    """The rule above is only meaningful if something does use FastAPI."""
    users = {
        path.relative_to(PACKAGE_ROOT).parts[0]
        for path in PACKAGE_ROOT.rglob("*.py")
        if "fastapi" in _imported_roots(path)
    }
    assert users == {"transport", "channels"}


def _modules_after(statement: str) -> set[str]:
    """The value of ``sys.modules`` in a fresh interpreter after one import."""
    source = f"import sys\n{statement}\nprint('\\n'.join(sorted(sys.modules)))\n"
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=True
    )
    return set(result.stdout.split())


def test_importing_the_toolbox_does_not_load_the_transport() -> None:
    """Using the toolbox must not construct the HTTP layer.

    Note what this does *not* assert. FastAPI itself will be in ``sys.modules``,
    because Agno's official AG-UI handlers live inside its AgentOS package and
    that package imports FastAPI — and reusing those handlers instead of
    reimplementing the chunk-to-event mapping is the central bet of this
    project. So the achievable invariant is about our own code: the transport
    module stays unimported until somebody asks for a router, which is what
    makes ``make_agui_router`` a lazy attribute on the package.
    """
    loaded = _modules_after("import agno_relay")
    assert "agno_relay.transport" not in loaded
    assert "agno_relay.transport.router" not in loaded


def test_importing_the_toolbox_does_not_load_the_observability_layer() -> None:
    """Tracing is an extra, so importing the package must not require it.

    As with the transport above, the assertion is about our own code rather
    than about OpenTelemetry's absence from ``sys.modules``: the redis client
    imports it for its own instrumentation, so in an environment that has both,
    it is loaded whatever we do. What we control is that nothing here reaches
    for it until somebody asks for tracing.
    """
    loaded = _modules_after("import agno_relay")
    assert "agno_relay.observability" not in loaded
    assert "agno_relay.observability.module" not in loaded


def test_the_observability_layer_loads_on_demand() -> None:
    loaded = _modules_after("import agno_relay; agno_relay.ObservabilityModule")
    assert "agno_relay.observability.module" in loaded


def test_the_transport_loads_on_demand() -> None:
    """The laziness above must not turn the router into a broken export."""
    loaded = _modules_after("import agno_relay; agno_relay.make_agui_router")
    assert "agno_relay.transport.router" in loaded


def test_driving_a_run_by_hand_does_not_load_the_transport() -> None:
    """The full-manual path, as documented, stays clear of the web layer."""
    loaded = _modules_after(
        "from agno_relay import EventTranslator, make_run_scope\n"
        "EventTranslator(scope=make_run_scope(thread_id='t', run_id='r'))"
    )
    assert "agno_relay.transport" not in loaded
