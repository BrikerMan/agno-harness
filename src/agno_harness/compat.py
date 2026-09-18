"""Install a top-level package alias that forwards onto ``agno_harness``.

Used by ``better_agno_toolbox``. Wrappers are distinct
module objects so CPython never mutates the real module's ``__spec__``.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types
from typing import Any

TARGET = "agno_harness"
TARGET_PREFIX = "agno_harness."

# Names that ``agno_harness`` resolves lazily so a plain import does not pull
# in FastAPI, OpenTelemetry, or Redis. Must not be copied out of ``vars()``.
LAZY_EXPORTS = frozenset(
    {
        "ConfigurationError",
        "ObservabilityModule",
        "RedisRunEventLog",
        "RelayServer",
        "make_agui_router",
        "make_relay_router",
        "setup_otlp",
    }
)


class AliasModule(types.ModuleType):
    """A package/module name that forwards every attribute to ``agno_harness``."""

    def __init__(self, name: str, target: types.ModuleType) -> None:
        super().__init__(name, target.__doc__)
        object.__setattr__(self, "_target", target)
        self.__file__ = getattr(target, "__file__", None)
        self.__package__ = name if hasattr(target, "__path__") else name.rpartition(".")[0]
        if hasattr(target, "__path__"):
            self.__path__ = list(target.__path__)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_target"), name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(dir(object.__getattribute__(self, "_target"))))


class _ExistingModuleLoader(importlib.abc.Loader):
    """Return an already-built wrapper instead of executing a new module."""

    def __init__(self, module: types.ModuleType) -> None:
        self._module = module

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        return self._module

    def exec_module(self, module: types.ModuleType) -> None:
        return None


class AliasFinder(importlib.abc.MetaPathFinder):
    """Map ``{alias}.*`` onto the corresponding ``agno_harness.*``."""

    def __init__(self, alias: str) -> None:
        self.alias = alias
        self.prefix = f"{alias}."

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: Any = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(self.prefix):
            return None
        existing = sys.modules.get(fullname)
        if existing is not None:
            return _spec_for(fullname, existing)
        real_name = TARGET_PREFIX + fullname[len(self.prefix) :]
        try:
            real = importlib.import_module(real_name)
        except ModuleNotFoundError:
            return None
        wrapper = AliasModule(fullname, real)
        sys.modules[fullname] = wrapper
        return _spec_for(fullname, wrapper)


def _spec_for(fullname: str, module: types.ModuleType) -> importlib.machinery.ModuleSpec:
    is_pkg = hasattr(module, "__path__")
    spec = importlib.util.spec_from_loader(
        fullname,
        _ExistingModuleLoader(module),
        origin=getattr(module, "__file__", None),
        is_package=is_pkg,
    )
    if spec is None:  # pragma: no cover - loader is always provided
        raise ImportError(f"could not build spec for {fullname}")
    if is_pkg:
        spec.submodule_search_locations = list(module.__path__)
    return spec


def install_alias_finder(alias: str) -> None:
    if any(isinstance(finder, AliasFinder) and finder.alias == alias for finder in sys.meta_path):
        return
    # Must run before PathFinder. Otherwise a parent alias's ``__path__``
    # would load ``longrun.py`` a second time under the alias name.
    sys.meta_path.insert(0, AliasFinder(alias))


def bind_alias(alias: str, module_globals: dict[str, Any]) -> None:
    """Wire an alias package's ``__init__`` onto ``agno_harness``."""
    real = importlib.import_module(TARGET)
    install_alias_finder(alias)
    for name, value in vars(real).items():
        if name in LAZY_EXPORTS:
            continue
        if name.startswith("_") and name not in {"__version__", "__all__"}:
            continue
        module_globals[name] = value
    module_globals["__version__"] = real.__version__

    def __getattr__(name: str) -> Any:
        return getattr(real, name)

    def __dir__() -> list[str]:
        return sorted(set(module_globals) | set(getattr(real, "__all__", ())))

    module_globals["__getattr__"] = __getattr__
    module_globals["__dir__"] = __dir__
    real.install_sealed_history_hook()
