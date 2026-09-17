"""The module contract: conflicts, ordering, degradation, and closed brackets.

The last of those is the one that matters most in production. A module that
opens a ``CUSTOM`` bracket owns closing it, because the sequencer only knows how
to close the bracket types the protocol defines. Get it wrong and a client that
saw ``ui.block.start`` waits forever for an end that is never coming — and it
only happens when a run dies mid-block, which is exactly when nobody is
watching.

So there is a generic test at the bottom that kills a run in the middle of each
module's work and asserts the output has no unmatched start. It is written
against the registry rather than against any particular module, so a module
added later is covered by it for free.
"""

from __future__ import annotations

import pytest
from ag_ui.core import BaseEvent, CustomEvent, EventType

from agno_relay import AguiRuntime, SequencerMode
from agno_relay.runtime.module import Module, ModuleConflict, ModuleRegistry
from agno_relay.runtime.modules.custom_events import CustomEventsModule
from agno_relay.runtime.modules.streamui import StreamUIModule
from agno_relay.runtime.modules.subagent import SubAgentModule
from agno_relay.runtime.translator import make_run_scope
from agno_relay.stores import InMemoryCustomEventStore, Stores

from .conformance import assert_valid_agui_sequence
from .conftest import FakeAgent, collect, content, customs, make_input, run_completed, types_of


def named(module_name: str, namespace: str | None = None) -> Module:
    module = Module()
    module.name = module_name
    module.namespace = namespace
    return module


class TestRegistryConflicts:
    def test_two_modules_cannot_share_a_name(self):
        registry = ModuleRegistry([named("ui")])
        with pytest.raises(ModuleConflict, match="named 'ui'"):
            registry.add(named("ui"))

    def test_two_modules_cannot_share_a_namespace(self):
        registry = ModuleRegistry([named("a", "ui.")])
        with pytest.raises(ModuleConflict, match="overlaps"):
            registry.add(named("b", "ui."))

    def test_a_namespace_cannot_be_a_prefix_of_another(self):
        """``ui.card.start`` would belong to both, and it has to belong to one."""
        registry = ModuleRegistry([named("a", "ui.")])
        with pytest.raises(ModuleConflict, match="overlaps"):
            registry.add(named("b", "ui.card."))

    def test_the_prefix_check_works_in_both_registration_orders(self):
        registry = ModuleRegistry([named("a", "ui.card.")])
        with pytest.raises(ModuleConflict, match="overlaps"):
            registry.add(named("b", "ui."))

    def test_modules_without_a_namespace_never_conflict(self):
        registry = ModuleRegistry([named("a"), named("b"), named("c")])
        assert len(registry) == 3

    def test_an_event_name_resolves_to_its_owner(self):
        owner = named("ui", "ui.")
        registry = ModuleRegistry([named("other", "subagent."), owner])
        assert registry.owner_of("ui.block.start") is owner
        assert registry.owner_of("billing") is None

    def test_the_built_in_modules_do_not_conflict(self):
        runtime = AguiRuntime(agent=FakeAgent([run_completed()]))
        names = [m.name for m in runtime.modules]
        assert names == ["subagent", "tool_filters", "streamui", "custom_events"]

    def test_a_registered_module_cannot_squat_a_built_in_namespace(self):
        runtime = AguiRuntime(agent=FakeAgent([run_completed()]))
        with pytest.raises(ModuleConflict):
            runtime.register_module(named("mine", "ui."))


class TestStaging:
    async def test_modules_stage_in_registration_order(self):
        order = []

        class Recorder(Module):
            def __init__(self, tag):
                self.name = tag
                self.namespace = None

            async def stage(self, event, run):
                order.append(self.name)
                yield event

        registry = ModuleRegistry([Recorder("first"), Recorder("second")])
        scope = make_run_scope(thread_id="t", run_id="r")
        event = CustomEvent(type=EventType.CUSTOM, name="x", value={})
        assert [e async for e in registry.stage(event, scope)] == [event]
        assert order == ["first", "second"]

    async def test_an_expansion_is_seen_by_the_next_module(self):
        """One event becoming two must not hide the second from downstream."""

        class Doubler(Module):
            name = "doubler"

            async def stage(self, event, run):
                yield event
                yield event

        seen = []

        class Counter(Module):
            name = "counter"

            async def stage(self, event, run):
                seen.append(event)
                yield event

        registry = ModuleRegistry([Doubler(), Counter()])
        scope = make_run_scope(thread_id="t", run_id="r")
        event = CustomEvent(type=EventType.CUSTOM, name="x", value={})
        out = [e async for e in registry.stage(event, scope)]
        assert len(out) == 2
        assert len(seen) == 2

    async def test_a_module_can_drop_an_event(self):
        class Dropper(Module):
            name = "dropper"

            async def stage(self, event, run):
                return
                yield  # pragma: no cover

        registry = ModuleRegistry([Dropper(), named("after")])
        scope = make_run_scope(thread_id="t", run_id="r")
        event = CustomEvent(type=EventType.CUSTOM, name="x", value={})
        assert [e async for e in registry.stage(event, scope)] == []

    def test_teardown_runs_in_reverse_registration_order(self):
        registry = ModuleRegistry([named("a"), named("b"), named("c")])
        assert [m.name for m in reversed(list(registry))] == ["c", "b", "a"]


class TestPerRunState:
    async def test_two_runs_do_not_share_module_state(self):
        """Modules are registered once and shared, so state lives on the run."""
        module = StreamUIModule()
        first = make_run_scope(thread_id="t", run_id="r1")
        second = make_run_scope(thread_id="t", run_id="r2")
        assert module._parser_for(first, "m") is not module._parser_for(second, "m")
        assert module._parser_for(first, "m") is module._parser_for(first, "m")


class TestCustomEventPersistence:
    async def test_an_unclaimed_custom_event_is_saved(self):
        stores = Stores(custom_events=InMemoryCustomEventStore())
        runtime = AguiRuntime(
            agent=FakeAgent([run_completed()]),
            stores=stores,
            sequencer_mode=SequencerMode.AUDIT,
        )

        async def notice(scope):
            yield CustomEvent(type=EventType.CUSTOM, name="quota", value={"left": 3})

        runtime.on_pre_run(notice)
        await collect(runtime.stream_events(make_input(thread_id="t1", run_id="r1")))

        saved = await stores.custom_events.list_by_thread("t1")
        assert [(row["name"], row["value"]) for row in saved] == [("quota", {"left": 3})]

    async def test_a_claimed_custom_event_is_left_to_its_owner(self):
        """UI blocks are rebuilt from the text, so a generic save would duplicate."""
        stores = Stores(custom_events=InMemoryCustomEventStore())
        runtime = AguiRuntime(
            agent=FakeAgent(
                [
                    content('```stream-ui {"schema": "demo-card"}\n{"kind":"card"}\n```\n'),
                    run_completed(),
                ]
            ),
            stores=stores,
            sequencer_mode=SequencerMode.AUDIT,
        )
        await collect(runtime.stream_events(make_input(thread_id="t1", run_id="r1")))

        saved = await stores.custom_events.list_by_thread("t1")
        assert [row["name"] for row in saved] == []

    async def test_no_store_degrades_to_live_only(self):
        """A missing store is a configuration, not an error."""
        runtime = AguiRuntime(
            agent=FakeAgent([run_completed()]), sequencer_mode=SequencerMode.AUDIT
        )

        async def notice(scope):
            yield CustomEvent(type=EventType.CUSTOM, name="quota", value={})

        runtime.on_pre_run(notice)
        events = await collect(runtime.stream_events(make_input()))
        assert customs(events, "quota")


# ── the generic bracket guarantee ─────────────────────────────────────────


def unmatched_brackets(events: list[BaseEvent], registry: ModuleRegistry) -> dict[str, int]:
    """Count ``*.start`` custom frames with no matching ``*.end``, per namespace."""
    open_counts: dict[str, int] = {}
    for event in events:
        if getattr(event, "type", None) is not EventType.CUSTOM:
            continue
        name = getattr(event, "name", "") or ""
        owner = registry.owner_of(name)
        if owner is None:
            continue
        if name.endswith(".start"):
            open_counts[owner.name] = open_counts.get(owner.name, 0) + 1
        elif name.endswith(".end"):
            open_counts[owner.name] = open_counts.get(owner.name, 0) - 1
    return {name: count for name, count in open_counts.items() if count != 0}


class TestBracketsAlwaysClose:
    """Every module closes what it opened, even when the run dies mid-way."""

    async def test_a_run_that_dies_inside_a_block_still_closes_it(self):
        runtime = AguiRuntime(
            # The fence opens and the model never finishes it, because the
            # stream raises instead of producing the closing ```.
            agent=FakeAgent(
                [content('```stream-ui {"schema": "demo-card"}\n{"kind":"card"}\n')],
                raise_at=1,
            ),
            sequencer_mode=SequencerMode.AUDIT,
        )
        events = await collect(runtime.stream_events(make_input()))

        assert types_of(events)[-1] == "RUN_ERROR"
        assert unmatched_brackets(events, runtime.modules) == {}
        end = customs(events, "ui.block.end")
        assert end and end[0].value["truncated"] is True

    async def test_a_run_that_dies_inside_a_delegation_still_closes_the_panel(self):
        from agno_relay import substream

        class DelegatingAgent(FakeAgent):
            async def _stream(self):
                yield content("thinking")
                async with substream("reviewer", description="checking"):
                    raise RuntimeError("model exploded")

        runtime = AguiRuntime(agent=DelegatingAgent([]), sequencer_mode=SequencerMode.AUDIT)
        events = await collect(runtime.stream_events(make_input()))

        assert types_of(events)[-1] == "RUN_ERROR"
        assert unmatched_brackets(events, runtime.modules) == {}
        end = customs(events, "subagent.end")
        assert end and end[0].value["interrupted"] is True

    async def test_a_healthy_run_is_balanced_too(self):
        runtime = AguiRuntime(
            agent=FakeAgent(
                [
                    content('```stream-ui {"schema": "demo-card"}\n{"kind":"card"}\n```\ndone'),
                    run_completed(),
                ]
            ),
            sequencer_mode=SequencerMode.AUDIT,
        )
        events = await collect(runtime.stream_events(make_input()))
        assert_valid_agui_sequence(events)
        assert unmatched_brackets(events, runtime.modules) == {}


class TestModuleDefaults:
    async def test_the_base_module_is_a_pass_through(self):
        module = Module()
        scope = make_run_scope(thread_id="t", run_id="r")
        event = CustomEvent(type=EventType.CUSTOM, name="x", value={})

        assert [e async for e in module.stage(event, scope)] == [event]
        assert await module.observe(event, scope) is None
        assert [e async for e in module.on_run_start(scope)] == []
        assert [e async for e in module.on_run_finish(scope, error=True)] == []
        assert module.claims_item(object()) is False

    def test_the_subagent_module_claims_only_its_own_items(self):
        module = SubAgentModule()
        assert module.claims_item(object()) is False

    def test_custom_events_claims_nothing_by_default(self):
        assert CustomEventsModule().namespace is None
