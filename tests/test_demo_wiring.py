"""The demo is a deliverable, so its wiring is tested like one.

Everything here goes through ``examples/demo/backend/app`` rather than
constructing a runtime directly. The unit tests already prove each extension
point works in isolation; what these check is that the demo actually installs
them — that the filter is registered against the right tool name, that the
billing hook reaches the store, that the fence stage is on.

A regression in this file means the demo lies about what the toolbox does,
which is worse than a plain bug.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from ag_ui.core import EventType

from agno_harness import SequencerMode

from .conformance import assert_valid_agui_sequence
from .conftest import (
    FakeAgent,
    content,
    customs,
    make_input,
    run_completed,
    text_of,
    tool_completed,
    tool_started,
    types_of,
)

DEMO_BACKEND = Path(__file__).resolve().parents[1] / "examples" / "demo" / "backend"

if not (DEMO_BACKEND / "app" / "main.py").is_file():
    pytest.skip(
        "examples/demo is local-only and not in the published tree", allow_module_level=True
    )

if str(DEMO_BACKEND) not in sys.path:
    sys.path.insert(0, str(DEMO_BACKEND))

# The demo pulls in agno's OpenAI model class, which is an optional extra.
app_agent_setup = pytest.importorskip("app.agent_setup")
app_main = pytest.importorskip("app.main")
app_models = pytest.importorskip("app.models")
app_scenarios = pytest.importorskip("app.scenarios")
app_tools = pytest.importorskip("app.tools")


@pytest.fixture
async def stores(tmp_path):
    """A file, not ``:memory:``.

    Every connection to an in-memory SQLite opens a *new* empty database, so
    once anything here reads and writes concurrently — which is exactly what a
    detached run and a client attached to it do — rows written by one session
    are invisible to the other, intermittently.

    The hot log is the same in-process stand-in the demo uses when Redis is
    unset: resume lives there, the SQL archive is written when the run settles.
    """
    from agno_harness.stores import InMemoryRunEventLog

    built, _ = await app_models.setup_persistence(f"sqlite+aiosqlite:///{tmp_path}/demo.db")
    hot = InMemoryRunEventLog()
    built.event_log = hot
    built.event_stream = hot
    return built


async def stream_demo(chunks, stores, **input_kwargs):
    """Run scripted chunks through the demo's own runtime configuration."""
    runtime = app_agent_setup.build_runtime(FakeAgent(chunks), None, stores)
    events = [event async for event in runtime.stream_events(make_input(**input_kwargs))]
    return runtime, events


class TestDemoRuntime:
    async def test_the_demo_audits_rather_than_silently_repairing(self, stores):
        runtime, _ = await stream_demo([content("hi"), run_completed()], stores)
        assert runtime.sequencer_mode is SequencerMode.AUDIT

    async def test_a_plain_run_is_well_formed(self, stores):
        _, events = await stream_demo([content("hi"), run_completed()], stores)
        assert_valid_agui_sequence(events)
        assert text_of(events) == "hi"

    async def test_the_hidden_tool_leaves_no_trace(self, stores):
        """Not just no tool frames -- no empty message Agno opened to host them."""
        _, events = await stream_demo(
            [
                tool_started("t1", "load_skill", {"name": "concise"}),
                tool_completed("t1", "load_skill", "loaded"),
                content("12 * 34 is 408."),
                run_completed(),
            ],
            stores,
        )
        assert_valid_agui_sequence(events)
        assert not [t for t in types_of(events) if t.startswith("TOOL_CALL")]
        assert types_of(events).count("TEXT_MESSAGE_START") == 1
        assert text_of(events) == "12 * 34 is 408."

    async def test_the_delegate_tool_shows_as_a_sub_agent_rather_than_a_card(self, stores):
        """The panel and the card would say the same thing, so only the panel ships."""
        _, events = await stream_demo(
            [
                tool_started(
                    "t1",
                    "delegate_subagent",
                    {
                        "agent_name": "reviewer",
                        "description": "Reviewing add()",
                        "prompt": "def add(a, b): ...",
                    },
                ),
                tool_completed("t1", "delegate_subagent", "The sign is wrong."),
                content("The reviewer found a sign error."),
                run_completed(),
            ],
            stores,
        )
        assert_valid_agui_sequence(events)
        assert not [t for t in types_of(events) if t.startswith("TOOL_CALL")]
        assert text_of(events) == "The reviewer found a sign error."

    async def test_a_delegation_is_bracketed_and_linked_to_its_tool_call(self, stores):
        """The delegating card is hidden, so the bracket is how a client finds
        the panel and the tool call id is how it puts it in the right place.

        What the sub-agent produced is not recorded separately: it is the frames
        between the two boundaries, which is where the client reads it from live
        and where frame replay reads it from afterwards.
        """
        from agno_harness import substream

        runtime = app_agent_setup.build_runtime(FakeAgent([]), None, stores)

        async def parent_stream(**kwargs):
            yield tool_started(
                "t1",
                "delegate_subagent",
                {"agent_name": "reviewer", "description": "Reviewing add()"},
            )
            async with substream(
                "reviewer", description="Reviewing add()", prompt="def add(a, b): ..."
            ) as emit:
                await emit(content("The sign is wrong."))
            yield tool_completed("t1", "delegate_subagent", "The sign is wrong.")
            yield content("The reviewer found a sign error.")
            yield run_completed()

        runtime.agent.arun = lambda **kwargs: parent_stream(**kwargs)
        events = [
            event
            async for event in runtime.stream_events(make_input(thread_id="t-sub", run_id="r-sub"))
        ]
        assert_valid_agui_sequence(events)

        started = customs(events, "subagent.start")[0].value
        assert started["description"] == "Reviewing add()"
        assert started["toolCallId"] == "t1"
        assert len(customs(events, "subagent.end")) == 1

    async def test_a_verbose_result_is_summarized_before_it_reaches_the_client(self, stores):
        verbose = json.dumps(
            {
                "topic": "latency",
                "rows": [{"id": i, "topic": "latency", "score": i / 100} for i in range(40)],
            }
        )
        _, events = await stream_demo(
            [
                tool_started("t1", "fetch_verbose_report", {"topic": "latency"}),
                tool_completed("t1", "fetch_verbose_report", verbose),
                content("Summarized."),
                run_completed(),
            ],
            stores,
        )
        assert_valid_agui_sequence(events)

        results = [e for e in events if e.type is EventType.TOOL_CALL_RESULT]
        assert len(results) == 1
        summary = json.loads(results[0].content)
        assert summary == {
            "topic": "latency",
            "rowCount": 40,
            "topScore": 0.39,
            "note": "Full payload withheld by TransformResultFilter.",
        }
        assert len(results[0].content) < len(verbose)

    async def test_a_result_it_cannot_parse_passes_through_rather_than_being_lost(self, stores):
        """The summarizer only understands its own tool's payload shape.

        The translator unwraps Agno's double-encoding, so a plain-text result
        arrives raw rather than as a quoted JSON string — and the summarizer,
        which cannot parse it, must hand it back untouched: a transform that
        cannot do its job has to be transparent, not destructive.
        """
        _, events = await stream_demo(
            [
                tool_started("t1", "fetch_verbose_report", {"topic": "latency"}),
                tool_completed("t1", "fetch_verbose_report", "the service returned an error"),
                run_completed(),
            ],
            stores,
        )
        results = [e for e in events if e.type is EventType.TOOL_CALL_RESULT]
        assert results[0].content == "the service returned an error"

    async def test_the_billing_hook_lands_after_run_started_and_is_persisted(self, stores):
        _, events = await stream_demo(
            [content("hello"), run_completed()], stores, thread_id="t-bill", run_id="r-bill"
        )
        types = types_of(events)
        billing = customs(events, "billing")

        assert types[0] == "RUN_STARTED"
        # The hook yields before the run opens; the sequencer holds it back.
        assert types[1] == "CUSTOM"
        assert len(billing) == 1
        assert billing[0].value["runId"] == "r-bill"

        saved = await stores.custom_events.list_by_thread("t-bill")
        assert [row["name"] for row in saved] == ["billing"]
        assert saved[0]["value"]["credits"] == 1

    async def test_the_streamui_fence_never_reaches_the_visible_text(self, stores):
        _, events = await stream_demo(
            [
                content("Here are the stats.\n"),
                content(
                    '```stream-ui {"schema": "stat-row"}\n'
                    '{"label": "Stars", "value": "15.2k", "trend": "up"}\n'
                ),
                content("```\nThat is all."),
                run_completed(),
            ],
            stores,
        )
        assert_valid_agui_sequence(events)

        visible = text_of(events)
        assert "stream-ui" not in visible
        assert "```" not in visible
        assert visible == "Here are the stats.\nThat is all."

        assert len(customs(events, "ui.block.start")) == 1
        lines = customs(events, "ui.item")
        assert [line.value["data"]["label"] for line in lines] == ["Stars"]
        assert lines[0].value["schema"] == "stat"
        assert customs(events, "ui.block.end")[0].value["truncated"] is False


class TestTodoWriter:
    """The plan is a card a tool rewrites, so both halves have to hold: the
    markdown the model writes must parse, and what it parses to must validate."""

    def test_each_checkbox_marker_maps_to_a_state(self):
        items = app_tools.parse_todo_list(
            "Here is the plan:\n"
            "- [ ] Read the spec\n"
            "- [-] Draft the middleware\n"
            "* [x] Pick an algorithm\n"
            "- [e] Load test\n"
            "\n"
            "- [?] Marker nobody documented\n"
        )
        assert items == [
            {"label": "Read the spec", "state": "pending"},
            {"label": "Draft the middleware", "state": "doing"},
            {"label": "Pick an algorithm", "state": "done"},
            {"label": "Load test", "state": "error"},
            # An unknown marker is one wrong icon, not a lost plan.
            {"label": "Marker nobody documented", "state": "pending"},
        ]

    def test_prose_without_a_checklist_is_reported_rather_than_drawn_empty(self):
        assert app_tools.parse_todo_list("I will start by reading the spec.") == []

    def test_nested_substeps_with_indentation(self):
        items = app_tools.parse_todo_list(
            "- [x] Parent task 1\n"
            "- [-] Parent task 2\n"
            "  - [x] Sub-task 2.1\n"
            "  - [-] Sub-task 2.2\n"
            "    - [ ] Deep sub-task\n"
            "- [ ] Parent task 3\n"
        )
        assert items == [
            {"label": "Parent task 1", "state": "done"},
            {"label": "Parent task 2", "state": "doing"},
            {"label": "Sub-task 2.1", "state": "done", "level": 1},
            {"label": "Sub-task 2.2", "state": "doing", "level": 1},
            {"label": "Deep sub-task", "state": "pending", "level": 2},
            {"label": "Parent task 3", "state": "pending"},
        ]

    async def test_the_tool_emits_one_validated_block_per_call(self, stores):
        async def parent(**kwargs):
            await app_tools.todo_write.entrypoint(
                todos="- [x] Pick an algorithm\n- [-] Add the middleware\n- [ ] Load test",
                title="Rate limiting",
            )
            yield content("Working on it.")
            yield run_completed()

        runtime = app_agent_setup.build_runtime(FakeAgent([]), None, stores)
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = [event async for event in runtime.stream_events(make_input())]
        assert_valid_agui_sequence(events)

        start = customs(events, "ui.block.start")[0].value
        assert start["schema"] == "todo-list"
        assert start["itemSchema"] == "todo"
        assert start["props"]["title"] == "Rate limiting"
        assert start.get("error") is None

        items = [item.value for item in customs(events, "ui.item")]
        assert [item["data"]["state"] for item in items] == ["done", "doing", "pending"]
        assert not [item for item in items if item.get("error")]
        assert text_of(events) == "Working on it."


class TestCodeExecutionTools:
    """bash and run_python tools allow asynchronous command and script execution."""

    async def test_bash_runs_command_and_captures_output(self):
        result = await app_tools.bash.entrypoint("echo 'hello from bash'")
        assert "[exit: 0" in result
        assert "hello from bash" in result

    async def test_run_python_executes_code_with_time_sleep(self):
        code = "import time; time.sleep(0.05); print('computed', 21 * 2)"
        result = await app_tools.run_python.entrypoint(code)
        assert "[exit: 0" in result
        assert "computed 42" in result


class TestResearchGraph:
    """The graph is a view of the sub-agent's own tool calls.

    Nothing here drives an LLM — the test *is* the researcher, calling the same
    tools in the same order a model would, which is exactly what makes the card
    testable now that no single function paints it.
    """

    async def test_each_search_grows_the_card_and_the_report_closes_it(self, stores, monkeypatch):
        from app import research as app_research
        from app.search_backends import SearchHit

        async def fake_search(query: str, *, num: int = 5):
            slug = query.split()[-1]
            return [
                SearchHit(
                    title=f"Result for {query}",
                    url=f"https://docs.ag-ui.com/{slug}",
                    snippet="AG-UI is an event-based protocol.",
                ),
                SearchHit(
                    title="Another source",
                    url=f"https://github.com/ag-ui-protocol/{slug}",
                    snippet="Open protocol for agent UIs.",
                ),
            ][:num], "exa"

        monkeypatch.setattr("app.research.search_web", fake_search)
        app_research.reset_trail(None)

        async def parent(**kwargs):
            await app_research.web_search.entrypoint(query="AG-UI protocol")
            await app_research.web_search.entrypoint(query="AG-UI adoption 2026")
            result = await app_research.finish_research.entrypoint(
                summary="Two independent sources describe the same event model."
            )
            yield content(result)
            yield run_completed()

        runtime = app_agent_setup.build_runtime(FakeAgent([]), None, stores)
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = [event async for event in runtime.stream_events(make_input())]
        assert_valid_agui_sequence(events)

        starts = [event.value for event in customs(events, "ui.block.start")]
        assert all(start["schema"] == "research-graph" for start in starts)
        # One card per tool call, each replacing the last.
        assert [start["props"]["phase"] for start in starts] == ["search", "search", "done"]

        items = [event.value for event in customs(events, "ui.item") if "data" in event.value]
        by_block: dict[str, list[dict]] = {}
        for item in items:
            by_block.setdefault(item["blockId"], []).append(item["data"])

        first, second, third = (by_block[start["blockId"]] for start in starts)

        # The graph accumulates server-side: the model never resends a node, so
        # it cannot quietly drop or invent one between searches.
        assert [node["kind"] for node in first] == ["query", "source", "source"]
        assert len(second) == 6
        assert [node["label"] for node in second if node["kind"] == "query"] == [
            "AG-UI protocol",
            "AG-UI adoption 2026",
        ]

        report = third[-1]
        assert report["kind"] == "report"
        assert report["label"].startswith("Two independent sources")
        # Every query feeds the report; that is the only thing the report claims.
        assert report["parent_ids"] == ["q1", "q2"]
        assert "insight" not in {node["kind"] for node in third}

        # Ranking is the engine's ordering, said as an ordinal.
        assert [node["rank"] for node in third if node["kind"] == "source"] == [1, 2, 1, 2]

        # Favicon comes from the resolver, not the tool.
        sourced = [
            item
            for item in items
            if item.get("data", {}).get("kind") == "source" and item.get("resolved")
        ]
        assert sourced
        assert "favicon" in sourced[0]["resolved"]

    async def test_a_page_two_queries_found_is_one_node_with_two_parents(self, monkeypatch):
        """Corroboration is the signal; two identical circles are not."""
        from app import research as app_research
        from app.search_backends import SearchHit

        async def fake_search(query: str, *, num: int = 5):
            return [
                # Returned twice by one query, and again by the next.
                SearchHit(title="Events", url="https://docs.ag-ui.com/events", snippet=""),
                SearchHit(title="Events", url="https://docs.ag-ui.com/events", snippet=""),
                SearchHit(title=f"Only for {query}", url=f"https://x.test/{query}", snippet=""),
            ], "exa"

        monkeypatch.setattr("app.research.search_web", fake_search)
        app_research.reset_trail(None)
        await app_research.web_search.entrypoint(query="one")
        await app_research.web_search.entrypoint(query="two")

        trail = app_research._trail(None)
        sources = [node for node in trail.nodes() if node["kind"] == "source"]
        assert len(sources) == 3
        shared = next(node for node in sources if node["url"] == "https://docs.ag-ui.com/events")
        assert shared["parent_ids"] == ["q1", "q2"]
        assert trail.source_count == 3

    async def test_the_search_budget_is_enforced_not_requested(self, monkeypatch):
        """A prompt asking for restraint is a suggestion; this is the limit."""
        from app import research as app_research
        from app.search_backends import SearchHit

        async def fake_search(query: str, *, num: int = 5):
            return [SearchHit(title=query, url=f"https://x.test/{query}", snippet="")], "exa"

        monkeypatch.setattr("app.research.search_web", fake_search)
        app_research.reset_trail(None)
        results = [
            await app_research.web_search.entrypoint(query=f"q{i}")
            for i in range(app_research._SEARCH_LIMIT + 2)
        ]

        assert len(app_research._trail(None).clusters) == app_research._SEARCH_LIMIT
        assert "budget spent" in results[-1]
        # It is told to wrap up well before it is cut off.
        assert "call finish_research now" in results[app_research._SEARCH_NUDGE - 1]

    async def test_finish_without_searching_is_refused(self, monkeypatch):
        from app import research as app_research

        app_research.reset_trail(None)
        result = await app_research.finish_research.entrypoint(summary="Made it up.")
        assert "web_search" in result


class TestResearcherSubAgent:
    """Research lives on the researcher sub-agent, not the parent."""

    def test_parent_delegates_research_tools_to_researcher(self, monkeypatch, tmp_path):
        from agno.db.sqlite import SqliteDb
        from agno.tools.user_feedback import UserFeedbackTools

        from agno_harness import SubAgentToolkit

        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        db = SqliteDb(db_file=str(tmp_path / "sessions.db"))
        from app.settings import LlmSettings

        settings = LlmSettings.from_env()
        agent, reviewer, researcher = app_agent_setup.build_agents(db, settings)

        assert reviewer.name == "reviewer"
        assert researcher.name == "researcher"

        def tool_names(a) -> set[str]:
            names: set[str] = set()
            for tool in a.tools or []:
                if isinstance(tool, SubAgentToolkit):
                    names.add(SubAgentToolkit.TOOL_NAME)
                    continue
                name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
                if name:
                    names.add(name)
            return names

        parent_names = tool_names(agent)
        assert "delegate_subagent" in parent_names
        assert any(isinstance(tool, UserFeedbackTools) for tool in agent.tools or [])
        assert not parent_names & {"web_search", "finish_research"}

        research_names = tool_names(researcher)
        assert research_names == {"web_search", "finish_research"}
        # Nothing that lets the model post a node list of its own: the trail is
        # accumulated server-side from real searches, so it cannot be forged.
        assert not {name for name in research_names if "graph" in name}

        toolkit = next(t for t in agent.tools if isinstance(t, SubAgentToolkit))
        assert set(toolkit.agents_by_name) == {"reviewer", "researcher"}


class TestModelSettings:
    """The demo has to be repointable at whatever endpoint you have a key for."""

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        # A key in the developer's own environment would mask the unconfigured
        # case, which is the one worth testing.
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DEMO_LLM_BASE_URL", "DEMO_MODEL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(app_main, "_load_dotenv", lambda: None)

        with TestClient(app_main.app) as client:
            yield client

    def test_it_boots_without_a_key_so_the_panel_can_ask_for_one(self, client):
        settings = client.get("/settings").json()
        assert settings == {
            "model": "gpt-4o-mini",
            "baseUrl": None,
            "hasApiKey": False,
            "apiKeyHint": None,
        }

    def test_agents_use_openai_like(self, client):
        from agno.models.openai.like import OpenAILike

        for agent in (
            app_main.state["agent"],
            app_main.state["reviewer"],
            app_main.state["researcher"],
        ):
            assert isinstance(agent.model, OpenAILike)

    def test_applying_settings_repoints_both_agents(self, client):
        response = client.put(
            "/settings",
            json={
                "model": "qwen-plus",
                "baseUrl": "https://example.test/v1",
                "apiKey": "sk-abcd1234",
            },
        )
        assert response.status_code == 200

        for agent in (
            app_main.state["agent"],
            app_main.state["reviewer"],
            app_main.state["researcher"],
        ):
            assert agent.model.id == "qwen-plus"
            assert agent.model.base_url == "https://example.test/v1"
            assert agent.model.api_key == "sk-abcd1234"

    def test_the_router_keeps_working_after_a_swap(self, client):
        """The runtime is mutated in place, so the mounted router stays valid."""
        before = client.get("/api/v1/health").json()["sequencerMode"]
        client.put("/settings", json={"model": "deepseek-chat", "apiKey": "sk-abcd1234"})

        assert client.get("/api/v1/health").json()["sequencerMode"] == before
        assert app_main.state["runtime"].agent is app_main.state["agent"]

    def test_the_key_is_write_only(self, client):
        response = client.put("/settings", json={"apiKey": "sk-supersecret-9999"})

        assert "supersecret" not in response.text
        assert response.json()["apiKeyHint"] == "…9999"
        assert "supersecret" not in client.get("/api/v1/health").text

    def test_a_partial_update_keeps_the_key(self, client):
        client.put("/settings", json={"apiKey": "sk-abcd1234"})
        response = client.put("/settings", json={"model": "deepseek-chat"})

        assert response.json()["hasApiKey"] is True
        assert app_main.state["settings"].api_key == "sk-abcd1234"

    def test_an_empty_base_url_clears_the_override(self, client):
        client.put("/settings", json={"baseUrl": "https://example.test/v1"})
        assert client.put("/settings", json={"baseUrl": ""}).json()["baseUrl"] is None
        # Cleared means "use the provider default", not "pass an empty host".
        assert "base_url" not in app_main.state["settings"].model_kwargs()


class TestResumeWiring:
    """The demo's answer to "what happens if I close this tab"."""

    async def test_a_detached_run_can_be_read_back_frame_for_frame(self, stores):
        """The contract the reloading frontend depends on, through the demo's own
        wiring: the same frames come back, in the same order, from storage."""
        from agno_harness.runtime.longrun import LongRunManager

        runtime = app_agent_setup.build_runtime(
            FakeAgent([content("hello"), run_completed()]), None, stores
        )
        long_runs = LongRunManager(runtime, log=stores.event_log)
        run_input = make_input(thread_id="t-detach", run_id="r-detach")

        await long_runs.start(run_input, user_id="alice")
        live = [frame async for frame in long_runs.attach("r-detach", user_id="alice")]

        stored = await runtime.threads.read_frames("t-detach", user_id="alice")
        assert [frame.event for frame in live] == [entry["event"] for entry in stored]
        assert [entry["id"] for entry in stored] == [f"r-detach:{f.offset}" for f in live]

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        monkeypatch.setattr(app_main, "_load_dotenv", lambda: None)
        monkeypatch.setattr(app_main, "DATA_DIR", tmp_path)
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'toolbox.db'}")
        monkeypatch.setenv("REDIS_URL", "memory://")
        with TestClient(app_main.app) as client:
            yield client

    def test_without_a_real_redis_the_demo_still_offers_live_resume(self, client):
        """An in-process stand-in is a Redis for one worker."""
        assert client.get("/api/v1/health").json()["resumeMode"] == "live"
        assert client.get("/api/v1/threads").headers.get("x-agui-resume") in (None, "live")

    def test_frames_replay_is_available_from_the_archive(self, client):
        response = client.get("/api/v1/threads/unknown-thread/frames")
        assert response.status_code == 200
        assert response.json() == {"frames": []}

    def test_identity_comes_from_the_header(self, client):
        """The one demo-only shortcut, and the only line a real app replaces."""
        request = type("R", (), {"headers": {"X-Demo-User": "alice"}})()
        assert app_main.resolve_user_id(request) == "alice"
        assert app_main.resolve_user_id(type("R", (), {"headers": {}})()) == "demo-user"


class TestScenarioCatalogue:
    def test_every_scenario_is_complete_and_uniquely_identified(self):
        scenarios = app_scenarios.SCENARIOS
        ids = [s["id"] for s in scenarios]

        assert len(ids) == len(set(ids))
        for scenario in scenarios:
            # The UI renders all four unconditionally, so a missing key is a
            # blank card rather than an error.
            assert {"id", "title", "group", "prompt", "watch"} <= scenario.keys()

    def test_grouping_preserves_catalogue_order(self):
        groups = app_scenarios.scenario_groups()
        flattened = [s["id"] for group in groups for s in group["scenarios"]]

        assert sorted(flattened) == sorted(s["id"] for s in app_scenarios.SCENARIOS)
        for group in groups:
            assert {s["group"] for s in group["scenarios"]} == {group["group"]}
