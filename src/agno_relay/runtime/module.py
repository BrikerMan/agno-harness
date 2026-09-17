"""The module contract.

A module is one optional capability with one file: sub-agent streaming,
generative-UI blocks, persisting injected events, recording a run for later
replay. Before this contract each of those was wired in by hand at a different
point in the pipeline, which is why a single feature's code ended up spread
across three files with nothing forcing the three to agree.

Four hooks, and no more than four. Each exists because something real needs it:

``stage``
    Rewrite, expand or drop an event on its way to the sequencer. This is where
    a fence becomes a block, and it is async because a module may have to look
    something up before it can finish the event.
``observe``
    Watch the sequencer's *output* — the frames the client actually received.
    Anything that records a run has to read from here, not from ``stage``, or it
    records events the sequencer went on to repair or discard.
``on_run_start`` / ``on_run_finish``
    Set up and tear down per-run state, and emit events around the run.

WHERE PER-RUN STATE GOES
------------------------
Modules are registered once and shared by every run the process serves, so a
module must not keep run state on ``self``. ``run.module_data(self.name)`` is a
private dict per (module, run); use it and concurrency takes care of itself.

CLOSING YOUR OWN BRACKETS
-------------------------
The sequencer closes unfinished text, tool and reasoning brackets when a run
ends, because those are events it understands. ``CUSTOM`` events pass through it
untouched, so a module that opens a bracket of its own owns closing it. This
matters most when the run dies mid-way: a client that received ``ui.block.start``
and never receives ``ui.block.end`` renders a spinner forever.

So the contract is explicit — ``on_run_finish`` must close every bracket the
module opened, whether the run succeeded or failed — and ``test_modules.py``
enforces it by killing a run in the middle of each module's work and asserting
the output contains no unmatched start.

Note that this protocol lives here rather than in ``core`` even though it is
mostly about events: its hooks take a :class:`RunScope`, and naming that type
means naming Agno's translator state. ``core`` stays free of Agno.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from typing import Any, Protocol, runtime_checkable

from ag_ui.core import BaseEvent
from agno.os.interfaces.agui.state import StreamState

from .scope import RunScope


class ChunkConverter(Protocol):
    """Turns one Agno chunk into events, the way the translator would.

    Handed to modules that process chunks from somewhere other than the main
    stream, so a sub-agent's output goes through the same handlers, parsers and
    tool filters as the parent's instead of a parallel implementation that
    drifts.
    """

    def __call__(self, chunk: Any, state: StreamState) -> Iterator[BaseEvent]: ...


@runtime_checkable
class BridgeModule(Protocol):
    """One optional capability, hooked into the event pipeline."""

    #: Unique among registered modules; also the key for per-run scratch space.
    name: str

    #: The ``CUSTOM`` event name prefix this module owns, e.g. ``"ui."``.
    #: ``None`` means the module emits no custom events of its own.
    namespace: str | None

    def stage(self, event: BaseEvent, run: RunScope) -> AsyncIterator[BaseEvent]: ...

    async def observe(self, event: BaseEvent, run: RunScope) -> None: ...

    def on_run_start(self, run: RunScope) -> AsyncIterator[BaseEvent]: ...

    def on_run_finish(self, run: RunScope, *, error: bool) -> AsyncIterator[BaseEvent]: ...

    def claims_item(self, item: Any) -> bool: ...

    def stage_item(
        self, item: Any, run: RunScope, *, convert: ChunkConverter
    ) -> AsyncIterator[BaseEvent]: ...


class Module:
    """Default no-op implementations, so a module overrides only what it needs."""

    name: str = "module"
    namespace: str | None = None

    async def stage(self, event: BaseEvent, run: RunScope) -> AsyncIterator[BaseEvent]:
        yield event

    async def observe(self, event: BaseEvent, run: RunScope) -> None:
        return None

    async def on_run_start(self, run: RunScope) -> AsyncIterator[BaseEvent]:
        return
        yield  # pragma: no cover - makes this an async generator

    async def on_run_finish(self, run: RunScope, *, error: bool) -> AsyncIterator[BaseEvent]:
        return
        yield  # pragma: no cover - makes this an async generator

    def claims_item(self, item: Any) -> bool:
        """Whether this module handles a non-chunk item from the runner.

        The runner's stream carries two kinds of thing: Agno chunks, which the
        translator converts, and out-of-band signals, which it does not
        understand. Sub-agent boundaries are the signal that exists today. A
        module claims those it recognises; everything unclaimed is treated as a
        chunk.
        """
        return False

    async def stage_item(
        self, item: Any, run: RunScope, *, convert: ChunkConverter
    ) -> AsyncIterator[BaseEvent]:
        return
        yield  # pragma: no cover - makes this an async generator

    def data(self, run: RunScope) -> dict[str, Any]:
        """This module's private scratch space for one run."""
        return run.module_data(self.name)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} namespace={self.namespace!r}>"


class ModuleConflict(ValueError):
    """Two modules claim the same name or overlapping custom-event namespaces."""


class ModuleRegistry:
    """An ordered set of modules, checked for conflicts at assembly time.

    Order matters for :meth:`stage`: each module sees what the previous one
    produced, so a module that rewrites text must run before one that parses it.
    Registration order is pipeline order.
    """

    def __init__(self, modules: Iterable[BridgeModule] = ()) -> None:
        self._modules: list[BridgeModule] = []
        for module in modules:
            self.add(module)

    def __iter__(self):
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)

    def __bool__(self) -> bool:
        return bool(self._modules)

    @property
    def modules(self) -> Sequence[BridgeModule]:
        return tuple(self._modules)

    def add(self, module: BridgeModule) -> ModuleRegistry:
        """Append a module, rejecting name and namespace collisions.

        Namespaces are checked by prefix in both directions. Registering ``ui.``
        alongside ``ui.card.`` is a conflict even though the strings differ: an
        event named ``ui.card.start`` would belong to both, and "which module
        owns this frame" has to have exactly one answer for the persistence and
        replay rules built on top of it.
        """
        self._check_name(module)
        self._check_namespace(module)
        self._modules.append(module)
        return self

    def _check_name(self, module: BridgeModule) -> None:
        clash = next((m for m in self._modules if m.name == module.name), None)
        if clash is not None:
            raise ModuleConflict(f"two modules are named {module.name!r}: {clash!r} and {module!r}")

    def _check_namespace(self, module: BridgeModule) -> None:
        namespace = module.namespace
        if not namespace:
            return
        for other in self._modules:
            existing = other.namespace
            if not existing:
                continue
            if namespace.startswith(existing) or existing.startswith(namespace):
                raise ModuleConflict(
                    f"module {module.name!r} claims namespace {namespace!r}, which overlaps "
                    f"{existing!r} already claimed by {other.name!r}"
                )

    def owner_of(self, event_name: str) -> BridgeModule | None:
        """The module whose namespace claims a ``CUSTOM`` event name."""
        for module in self._modules:
            if module.namespace and event_name.startswith(module.namespace):
                return module
        return None

    def item_owner(self, item: Any) -> BridgeModule | None:
        """The module that claims a non-chunk item, if any."""
        for module in self._modules:
            if module.claims_item(item):
                return module
        return None

    # ── pipeline ──────────────────────────────────────────────────────────

    async def stage(self, event: BaseEvent, run: RunScope) -> AsyncIterator[BaseEvent]:
        """Pass one event through every module, in registration order.

        A module may fan one event out into several; each of those is then fed
        to the *next* module, so expansions compose rather than shadow one
        another.
        """
        current = [event]
        for module in self._modules:
            if not current:
                return
            nxt: list[BaseEvent] = []
            for item in current:
                async for produced in module.stage(item, run):
                    nxt.append(produced)
            current = nxt
        for item in current:
            yield item

    async def observe(self, event: BaseEvent, run: RunScope) -> None:
        for module in self._modules:
            await module.observe(event, run)

    async def on_run_start(self, run: RunScope) -> AsyncIterator[BaseEvent]:
        for module in self._modules:
            async for event in module.on_run_start(run):
                yield event

    async def on_run_finish(self, run: RunScope, *, error: bool) -> AsyncIterator[BaseEvent]:
        """Tear down in reverse registration order.

        Reverse because teardown mirrors setup: a module registered later may
        depend on one registered earlier still being intact while it closes.
        """
        for module in reversed(self._modules):
            async for event in module.on_run_finish(run, error=error):
                yield event


__all__ = ["BridgeModule", "Module", "ModuleConflict", "ModuleRegistry"]
