"""``better_agno_toolbox`` is an alias for ``agno_harness``.

Downstream hosts keep their existing imports. These tests lock the public
paths they actually use, and assert the alias returns the *same object* as
the real module — a copy would break ``isinstance`` checks and catalog
registries.
"""

from __future__ import annotations

import subprocess
import sys


def test_package_import_and_version() -> None:
    import agno_harness
    import better_agno_toolbox

    assert better_agno_toolbox.__version__ == agno_harness.__version__
    assert better_agno_toolbox.AgentRuntime is agno_harness.AgentRuntime
    assert better_agno_toolbox.SmartCompressionManager is agno_harness.SmartCompressionManager
    assert better_agno_toolbox.StreamingArtifactToolkit is agno_harness.StreamingArtifactToolkit
    assert better_agno_toolbox.TodoToolkit is agno_harness.TodoToolkit
    assert better_agno_toolbox.SubAgentToolkit is agno_harness.SubAgentToolkit
    assert better_agno_toolbox.RunScope is agno_harness.RunScope
    assert better_agno_toolbox.ArtifactCard is agno_harness.ArtifactCard
    assert better_agno_toolbox.PresentationDeck is agno_harness.PresentationDeck


def test_lazy_router_matches() -> None:
    import agno_harness
    import better_agno_toolbox

    assert better_agno_toolbox.make_agui_router is agno_harness.make_agui_router


def test_submodule_identity() -> None:
    import better_agno_toolbox.core.streamui as alias_streamui
    import better_agno_toolbox.runtime.longrun as alias_longrun
    import better_agno_toolbox.stores as alias_stores

    import agno_harness.core.streamui as real_streamui
    import agno_harness.runtime.longrun as real_longrun
    import agno_harness.stores as real_stores

    # Wrappers are distinct module objects so CPython never mutates the real
    # ``__spec__``. Public classes and functions must still be identical.
    assert alias_streamui.CardCatalog is real_streamui.CardCatalog
    assert alias_longrun.LongRunManager is real_longrun.LongRunManager
    assert alias_stores.InMemoryRunEventLog is real_stores.InMemoryRunEventLog
    assert alias_stores.Stores is real_stores.Stores


def test_desktop_host_imports() -> None:
    """Public ``better_agno_toolbox`` imports used by a desktop sidecar host."""
    from better_agno_toolbox.core.streamui import (  # noqa: F401
        BlockSchema,
        CardCatalog,
        ItemSchema,
    )
    from better_agno_toolbox.runtime import make_run_scope  # noqa: F401
    from better_agno_toolbox.runtime.hitl import detect_resume  # noqa: F401
    from better_agno_toolbox.runtime.longrun import LongRunManager  # noqa: F401
    from better_agno_toolbox.runtime.module import ChunkConverter, Module  # noqa: F401
    from better_agno_toolbox.runtime.runner import AgentRunner  # noqa: F401
    from better_agno_toolbox.runtime.scope import RunScope as Scope  # noqa: F401
    from better_agno_toolbox.runtime.sidechannel import (  # noqa: F401
        SideChannel,
        bind_channel,
        merge_side_channel,
    )
    from better_agno_toolbox.runtime.titles import _persist_title  # noqa: F401
    from better_agno_toolbox.stores import InMemoryRunEventLog, Stores  # noqa: F401
    from better_agno_toolbox.stores.memory_log import RunStatus  # noqa: F401

    from better_agno_toolbox import (  # noqa: F401
        AgentRuntime,
        ArtifactCard,
        ObservabilityModule,
        PresentationDeck,
        RunScope,
        SmartCompressionManager,
        StreamingArtifactToolkit,
        SubAgentToolkit,
        TodoToolkit,
        make_agui_router,
        setup_otlp,
    )


def test_web_host_imports() -> None:
    """Public ``better_agno_toolbox`` imports used by a web Agent API host."""
    from better_agno_toolbox.core.log import RunStatus  # noqa: F401
    from better_agno_toolbox.core.streamui import BlockSchema, ItemSchema  # noqa: F401
    from better_agno_toolbox.observability import patch_context_detach  # noqa: F401
    from better_agno_toolbox.runtime import LongRunManager  # noqa: F401
    from better_agno_toolbox.runtime.module import Module  # noqa: F401
    from better_agno_toolbox.runtime.modules.streamui import (  # noqa: F401
        StreamUIModule,
        resolve_artifact_dir,
    )
    from better_agno_toolbox.runtime.replay import run_to_messages  # noqa: F401
    from better_agno_toolbox.runtime.scope import current_scope  # noqa: F401
    from better_agno_toolbox.runtime.threads import ThreadService  # noqa: F401
    from better_agno_toolbox.stores import (  # noqa: F401
        CustomEventMixin,
        CustomEventStore,
        HistoryArchive,
        RunArchiveMixin,
        RunRecordMixin,
        StoreRegistry,
        Stores,
    )
    from better_agno_toolbox.stores.redis_log import RedisRunEventLog  # noqa: F401
    from better_agno_toolbox.tools.artifact import SlideProgressItem  # noqa: F401
    from better_agno_toolbox.tools.subagent import SubAgentToolkit, substream  # noqa: F401
    from better_agno_toolbox.tools.todo import (  # noqa: F401
        TodoToolkit,
        format_todo_list,
        parse_todo_list,
    )
    from better_agno_toolbox.transport import make_agui_router  # noqa: F401

    from better_agno_toolbox import (  # noqa: F401
        CardCatalog,
        CardSchema,
        SkillManager,
        SmartCompressionManager,
        StreamingArtifactToolkit,
        emit_item,
        ui_block,
    )


def test_demo_backend_imports() -> None:
    """Imports used by the copied better-agno demo backend."""
    from better_agno_toolbox.core.streamui import CardCatalog  # noqa: F401
    from better_agno_toolbox.runtime.longrun import LongRunManager  # noqa: F401
    from better_agno_toolbox.stores import InMemoryRunEventLog, Stores  # noqa: F401
    from better_agno_toolbox.stores.redis_log import RedisRunEventLog  # noqa: F401
    from better_agno_toolbox.tools.subagent import SubAgentToolkit as _Sub  # noqa: F401

    from better_agno_toolbox import (  # noqa: F401
        AgentRuntime,
        HideToolFilter,
        ObservabilityModule,
        RunScope,
        SequencerMode,
        SerperTools,
        SmartCompressionManager,
        StreamingArtifactToolkit,
        SubAgentToolkit,
        TransformResultFilter,
        make_agui_router,
        make_checkpoint_hook,
        make_thread_title_hook,
        make_thread_title_pre_hook,
        setup_otlp,
    )


def _modules_after(statement: str) -> set[str]:
    source = f"import sys\n{statement}\nprint('\\n'.join(sorted(sys.modules)))\n"
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=True
    )
    return set(result.stdout.split())


def test_importing_shim_does_not_load_transport() -> None:
    loaded = _modules_after("import better_agno_toolbox")
    assert "agno_harness.transport" not in loaded
    assert "agno_harness.transport.router" not in loaded


def test_importing_shim_does_not_load_observability() -> None:
    loaded = _modules_after("import better_agno_toolbox")
    assert "agno_harness.observability" not in loaded
    assert "better_agno_toolbox.observability" not in loaded
