"""The FastAPI router: SSE framing, history endpoints, debug endpoints."""

from __future__ import annotations

import json

import pytest
from ag_ui.core import EventType
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agno_harness import AgentRuntime, SequencerMode, make_agui_router
from agno_harness.core.debug import EVENT_DEBUG_SUMMARY
from agno_harness.core.protocol import WIRE_PROTOCOL_VERSION
from agno_harness.runtime.longrun import LongRunManager
from agno_harness.stores import InMemoryRunEventLog, Stores
from agno_harness.transport.router import (
    PROTOCOL_HEADER,
    RESUME_HEADER,
    SSE_HEADERS,
)

from .conftest import FakeAgent, content, make_input, run_completed, tool_started
from .test_longrun import _HistoryOnlyLog as HistoryOnlyLog
from .test_replay import FakeDb, FakeInput, FakeRun, FakeSession


def make_client(chunks=None, db=None, **kwargs):
    runtime = AgentRuntime(
        agent=FakeAgent(chunks if chunks is not None else [content("hi"), run_completed()]),
        db=db,
        sequencer_mode=SequencerMode.AUDIT,
    )
    app = FastAPI()
    kwargs.setdefault("allow_anonymous", True)
    app.include_router(make_agui_router(runtime, **kwargs))
    return TestClient(app), runtime


def parse_sse(body: str) -> list[dict]:
    """Pull the JSON payloads out of an SSE body."""
    return [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]


def post_run(client, **kwargs):
    payload = make_input(**kwargs).model_dump(by_alias=True, mode="json")
    return client.post("/agui", json=payload)


class TestSSE:
    def test_a_run_streams_events_as_sse(self):
        client, _ = make_client()
        response = post_run(client)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)
        assert [e["type"] for e in events] == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]

    def test_buffering_is_disabled_for_proxies(self):
        client, _ = make_client()
        response = post_run(client)
        for header, value in SSE_HEADERS.items():
            assert response.headers.get(header) == value

    def test_a_malformed_body_is_rejected(self):
        client, _ = make_client()
        assert client.post("/agui", json={"nonsense": True}).status_code == 422


class TestDebugMode:
    def test_debug_appends_a_summary_frame(self):
        client, _ = make_client()
        events = parse_sse(client.post("/agui?debug=1", json=_payload()).text)
        assert events[-1]["type"] == "CUSTOM"
        assert events[-1]["name"] == EVENT_DEBUG_SUMMARY
        summary = events[-1]["value"]
        assert summary["frameCount"] == 5
        assert summary["countsByType"]["TEXT_MESSAGE_CONTENT"] == 1
        assert summary["timeToFirstContentMs"] is not None

    def test_the_summary_comes_after_the_terminal_event(self):
        client, _ = make_client()
        events = parse_sse(client.post("/agui?debug=1", json=_payload()).text)
        assert events[-2]["type"] == "RUN_FINISHED"

    def test_without_debug_the_stream_is_unchanged(self):
        client, _ = make_client()
        plain = parse_sse(post_run(client).text)
        assert all(e.get("name") != EVENT_DEBUG_SUMMARY for e in plain)

    def test_the_summary_reports_protocol_repairs(self):
        client, _ = make_client([tool_started("c1", "f"), run_completed()])
        events = parse_sse(client.post("/agui?debug=1", json=_payload()).text)
        rules = [v["rule"] for v in events[-1]["value"]["violations"]]
        assert "empty_text_message" in rules


class TestHistoryRoutes:
    @pytest.fixture
    def client(self):
        db = FakeDb([FakeSession("t1", runs=[FakeRun("r1", FakeInput("hello"), "Hi there.")])])
        client, _ = make_client(db=db)
        return client

    def test_threads_are_listed(self, client):
        threads = client.get("/threads").json()
        assert [t["threadId"] for t in threads] == ["t1"]
        assert threads[0]["title"] == "hello"

    def test_messages_are_replayed(self, client):
        messages = client.get("/threads/t1/messages").json()
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["content"] == "Hi there."

    def test_an_unknown_thread_is_a_404(self, client):
        assert client.get("/threads/nope/messages").status_code == 404

    def test_a_thread_can_be_deleted(self, client):
        assert client.delete("/threads/t1").json()["ok"] is True
        assert client.get("/threads").json() == []


class TestDebugRoutes:
    """The debug routes are opt-in — they serve raw model output."""

    @staticmethod
    def debug_client(chunks=None):
        return make_client(chunks, expose_debug_routes=True)

    def test_debug_routes_are_off_unless_asked_for(self):
        client, _ = make_client()
        assert client.get("/debug/chunks").status_code == 404

    def test_chunk_samples_are_exposed(self):
        client, _ = self.debug_client()
        post_run(client)
        payload = client.get("/debug/chunks").json()
        by_type = {t["eventType"]: t for t in payload["types"]}
        # The completion chunk is sampled too -- knowing its shape matters just
        # as much as knowing a content chunk's.
        assert set(by_type) == {"RunContent", "RunCompleted"}
        assert by_type["RunContent"]["samples"][0]["payload"]["content"] == "hi"

    def test_the_stream_state_of_a_run_is_exposed(self):
        client, _ = self.debug_client()
        post_run(client)
        assert client.get("/debug/state/run-1").json()["runId"] == "run-1"

    def test_an_unknown_run_is_a_404(self):
        client, _ = self.debug_client()
        assert client.get("/debug/state/nope").status_code == 404

    def test_violations_are_exposed(self):
        client, _ = self.debug_client([tool_started("c1", "f"), run_completed()])
        post_run(client)
        payload = client.get("/debug/violations").json()
        assert payload["mode"] == "audit"
        assert "empty_text_message" in [v["rule"] for v in payload["violations"]]

    def test_one_run_can_be_inspected_on_its_own(self):
        """The per-run view is what stays correct when runs overlap."""
        client, _ = self.debug_client([tool_started("c1", "f"), run_completed()])
        post_run(client)
        report = client.get("/debug/runs/run-1").json()
        assert report["runId"] == "run-1"
        assert report["streamState"]["runId"] == "run-1"
        assert "empty_text_message" in [v["rule"] for v in report["violations"]]
        assert "RunCompleted" in {t["eventType"] for t in report["chunks"]["types"]}

    def test_an_unknown_run_report_is_a_404(self):
        client, _ = self.debug_client()
        assert client.get("/debug/runs/nope").status_code == 404


class TestLongRunRoutes:
    """Detach, attach and cancel, once a LongRunManager is wired in."""

    @staticmethod
    def long_run_client(chunks=None):
        log = InMemoryRunEventLog()
        runtime = AgentRuntime(
            agent=FakeAgent(chunks if chunks is not None else [content("hi"), run_completed()]),
            stores=Stores(event_log=log),
            sequencer_mode=SequencerMode.AUDIT,
        )
        manager = LongRunManager(runtime, log=log)
        app = FastAPI()
        app.include_router(make_agui_router(runtime, long_runs=manager))
        return TestClient(app), manager

    def test_the_routes_do_not_exist_without_a_manager(self):
        """A 404 on the route beats a 500 halfway through a stream."""
        client, _ = make_client()
        assert client.get("/runs/run-1/attach").status_code == 404
        assert client.get("/runs/run-1/stream").status_code == 404
        assert client.post("/runs/run-1/abort").status_code == 404

    def test_a_detached_run_streams_the_same_events_back(self):
        client, _ = self.long_run_client()
        events = parse_sse(client.post("/agui?detach=1", json=_payload()).text)
        assert [e["type"] for e in events] == [
            "RUN_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]

    def test_frames_carry_their_offset_as_the_sse_id(self):
        """Which is what lets EventSource resume without any client bookkeeping."""
        client, _ = self.long_run_client()
        body = client.post("/agui?detach=1", json=_payload()).text
        ids = [line[len("id: ") :] for line in body.splitlines() if line.startswith("id: ")]
        assert len(ids) == 5
        assert len(set(ids)) == 5

    def test_a_run_can_be_replayed_from_the_beginning(self):
        """The fresh-page-load case: no cursor, the whole run."""
        client, _ = self.long_run_client()
        client.post("/agui?detach=1", json=_payload())

        events = parse_sse(client.get("/runs/run-1/attach").text)
        assert events[0]["type"] == "RUN_STARTED"
        assert events[-1]["type"] == "RUN_FINISHED"

    def test_deprecated_stream_alias_still_attaches(self):
        client, _ = self.long_run_client()
        client.post("/agui?detach=1", json=_payload())
        events = parse_sse(client.get("/runs/run-1/stream").text)
        assert events[0]["type"] == "RUN_STARTED"
        assert events[-1]["type"] == "RUN_FINISHED"

    def test_a_run_can_be_resumed_from_an_offset(self):
        client, _ = self.long_run_client()
        body = client.post("/agui?detach=1", json=_payload()).text
        second = [line[len("id: ") :] for line in body.splitlines() if line.startswith("id: ")][1]

        events = parse_sse(client.get(f"/runs/run-1/attach?after={second}").text)
        assert [e["type"] for e in events] == [
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "RUN_FINISHED",
        ]

    def test_last_event_id_is_honoured_like_an_after_cursor(self):
        """A browser sends it automatically; ignoring it would replay the run."""
        client, _ = self.long_run_client()
        body = client.post("/agui?detach=1", json=_payload()).text
        last = [line[len("id: ") :] for line in body.splitlines() if line.startswith("id: ")][-1]

        events = parse_sse(client.get("/runs/run-1/attach", headers={"Last-Event-ID": last}).text)
        assert events == []

    def test_the_resume_mode_is_advertised(self):
        client, _ = self.long_run_client()
        response = client.post("/agui?detach=1", json=_payload())
        assert response.headers[RESUME_HEADER] == "live"

    def test_a_runtime_without_long_runs_says_so(self):
        client, _ = make_client()
        assert post_run(client).headers[RESUME_HEADER] == "none"

    def test_health_is_opt_in_and_advertises_resume_mode(self):
        client, _ = make_client()
        assert client.get("/health").status_code == 404

        log = InMemoryRunEventLog()
        runtime = AgentRuntime(
            agent=FakeAgent([content("hi"), run_completed()]),
            stores=Stores(event_log=log),
        )
        manager = LongRunManager(runtime, log=log)
        app = FastAPI()
        app.include_router(
            make_agui_router(
                runtime,
                long_runs=manager,
                include_health=True,
                allow_anonymous=True,
            )
        )
        body = TestClient(app).get("/health").json()
        assert body["status"] == "healthy"
        assert body["resumeMode"] == "live"

    def test_active_runs_are_listed(self):
        client, _ = self.long_run_client()
        client.post("/agui?detach=1", json=_payload())
        assert client.get("/threads/thread-1/active").json() == []

    def test_long_run_query_parameters_work(self):
        client, _ = self.long_run_client()
        # ?long-run=1
        r1 = client.post("/agui?long-run=1", json=_payload())
        assert r1.status_code == 200
        events1 = parse_sse(r1.text)
        assert events1[0]["type"] == "RUN_STARTED"
        assert events1[0]["rawEvent"]["user_input"] == "hello"

        # ?long_run=1
        payload2 = make_input(text="world", run_id="run-2").model_dump(by_alias=True, mode="json")
        r2 = client.post("/agui?long_run=1", json=payload2)
        assert r2.status_code == 200
        events2 = parse_sse(r2.text)
        assert events2[0]["type"] == "RUN_STARTED"
        assert events2[0]["rawEvent"]["user_input"] == "world"

    def test_active_run_includes_user_input(self):
        log = InMemoryRunEventLog()
        runtime = AgentRuntime(agent=FakeAgent([]), stores=Stores(event_log=log))
        manager = LongRunManager(runtime, log=log)
        app = FastAPI()
        app.include_router(make_agui_router(runtime, long_runs=manager))
        client = TestClient(app)

        import asyncio

        asyncio.run(log.start_run("q-1", "t-1", input="what is quantum computing?"))

        active = client.get("/threads/t-1/active").json()
        assert len(active) == 1
        assert active[0]["runId"] == "q-1"
        assert active[0]["input"] == "what is quantum computing?"

    def test_aborting_a_finished_run_reports_nothing_to_do(self):
        client, _ = self.long_run_client()
        client.post("/agui?detach=1", json=_payload())
        assert client.post("/runs/run-1/abort").json() == {"aborted": False}

    def test_another_users_run_is_a_404_and_not_a_403(self):
        """403 would confirm the run exists, which is more than a stranger knew."""
        log = InMemoryRunEventLog()
        runtime = AgentRuntime(agent=FakeAgent([run_completed()]), stores=Stores(event_log=log))
        manager = LongRunManager(runtime, log=log)
        app = FastAPI()
        users = iter(["alice", "bob"])
        app.include_router(
            make_agui_router(runtime, long_runs=manager, resolve_user_id=lambda _: next(users))
        )
        client = TestClient(app)

        client.post("/agui?detach=1", json=_payload())
        assert client.post("/runs/run-1/abort").status_code == 404


class TestDetachedStreaming:
    @pytest.mark.parametrize(
        "log", [InMemoryRunEventLog(), HistoryOnlyLog()], ids=["live", "history"]
    )
    def test_a_detached_post_streams_the_run_it_just_started(self, log):
        """The regression that made the demo look broken.

        Detaching hands the run to a background task and streams it back out of
        the log. If that stream only ever contained what had already been
        written, the response would end almost immediately and the answer would
        appear only on the next reload — which is exactly what happened.

        Both parameters matter, and the second one more: a log that cannot tail
        is the demo's default, and it is the one that was broken.
        """
        runtime = AgentRuntime(
            agent=FakeAgent([content("hello "), content("there"), run_completed()]),
            stores=Stores(event_log=log),
        )
        app = FastAPI()
        app.include_router(make_agui_router(runtime, long_runs=LongRunManager(runtime, log=log)))

        with TestClient(app) as client:
            response = client.post(
                "/agui?detach=1", json=make_input().model_dump(by_alias=True, mode="json")
            )
            events = parse_sse(response.text)

        assert [e["type"] for e in events][-1] == EventType.RUN_FINISHED.value
        assert "".join(e.get("delta", "") for e in events) == "hello there"

    def test_every_frame_carries_the_offset_a_client_resumes_from(self):
        log = InMemoryRunEventLog()
        runtime = AgentRuntime(
            agent=FakeAgent([content("hi"), run_completed()]), stores=Stores(event_log=log)
        )
        app = FastAPI()
        app.include_router(make_agui_router(runtime, long_runs=LongRunManager(runtime, log=log)))

        with TestClient(app) as client:
            body = client.post(
                "/agui?detach=1", json=make_input().model_dump(by_alias=True, mode="json")
            ).text

        ids = [line[3:].strip() for line in body.splitlines() if line.startswith("id:")]
        assert ids and ids == sorted(ids)


class TestFramesRoute:
    def test_without_a_log_the_route_explains_itself(self):
        """501 with a reason, not an empty list that reads as "nothing happened"."""
        client, _ = make_client()
        response = client.get("/threads/t1/frames")
        assert response.status_code == 501
        assert (
            "RunEventLog" in response.json()["error"]
            or "history archive" in response.json()["error"]
        )

    def test_a_hot_layer_alone_is_replayable(self):
        runtime = AgentRuntime(
            agent=FakeAgent([run_completed()]), stores=Stores(event_log=InMemoryRunEventLog())
        )
        app = FastAPI()
        app.include_router(make_agui_router(runtime))
        response = TestClient(app).get("/threads/t1/frames")
        assert response.status_code == 200
        assert response.json() == {"frames": []}


class TestProtocolVersion:
    """A version skew should be one clear message, not a scrambled transcript."""

    def test_the_version_is_on_the_response(self):
        client, _ = make_client()
        assert post_run(client).headers[PROTOCOL_HEADER] == WIRE_PROTOCOL_VERSION

    def test_the_version_is_on_the_first_frame_too(self):
        """For clients that see the frames but not the headers — a replayed log,
        a proxy, a recorded trace."""
        client, _ = make_client()
        started = parse_sse(post_run(client).text)[0]
        assert started["rawEvent"]["protocol"] == WIRE_PROTOCOL_VERSION
        assert started["rawEvent"]["user_input"] == "hello"


class TestConfiguration:
    def test_a_prefix_is_applied(self):
        client, _ = make_client(prefix="/api")
        assert client.post("/api/agui", json=_payload()).status_code == 200
        assert client.post("/agui", json=_payload()).status_code == 404

    def test_the_user_id_is_resolved_server_side(self):
        runtime = AgentRuntime(agent=FakeAgent([run_completed()]))
        app = FastAPI()
        app.include_router(
            make_agui_router(runtime, resolve_user_id=lambda request: "user-from-header")
        )
        TestClient(app).post("/agui", json=_payload())
        assert runtime.agent.last_kwargs["user_id"] == "user-from-header"


def _payload():
    return make_input().model_dump(by_alias=True, mode="json")
