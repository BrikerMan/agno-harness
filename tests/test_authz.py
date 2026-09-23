"""Who can see what.

Before this, three routes took only a thread id: ``GET /threads``, ``GET
/threads/{id}/messages`` and ``DELETE /threads/{id}``. Thread ids are generated
by the client, so anyone holding one — or guessing one — could read or delete
somebody else's conversation, and listing returned everybody's.

Two rules are being pinned down here, and the second is the one that is easy to
get subtly wrong:

1. The identity comes from the server, never from the request body. Otherwise
   "who am I" is a field the caller fills in.
2. The identity is pushed *into the query*. Fetching first and comparing after
   works right up until one branch forgets, and even when it does not, a
   distinct "forbidden" answer confirms the thread exists. Everything unowned
   looks exactly like everything nonexistent: 404.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agno_harness import AgentRuntime, make_agui_router
from agno_harness.runtime.longrun import LongRunManager, RunNotOwned
from agno_harness.runtime.threads import ThreadService
from agno_harness.stores import InMemoryRunEventLog, Stores

from .conftest import FakeAgent, make_input, run_completed
from .test_replay import FakeDb, FakeInput, FakeRun, FakeSession

USER_HEADER = "x-user-id"


def two_user_db() -> FakeDb:
    return FakeDb(
        [
            FakeSession(
                "alice-thread",
                runs=[FakeRun("r1", FakeInput("alice's secret"), "kept")],
                user_id="alice",
            ),
            FakeSession(
                "bob-thread",
                runs=[FakeRun("r2", FakeInput("bob's secret"), "kept")],
                user_id="bob",
            ),
        ]
    )


def make_app(db=None, *, long_runs=None, runtime=None):
    """A router whose identity comes from a header, as a middleware would supply."""
    runtime = runtime or AgentRuntime(agent=FakeAgent([run_completed()]), db=db)
    app = FastAPI()
    app.include_router(
        make_agui_router(
            runtime,
            long_runs=long_runs,
            resolve_user_id=lambda request: request.headers.get(USER_HEADER),
        )
    )
    return TestClient(app), runtime


def as_user(client: TestClient, user: str):
    client.headers[USER_HEADER] = user
    return client


class TestThreadIsolation:
    @pytest.fixture
    def client(self):
        client, _ = make_app(two_user_db())
        return client

    def test_a_listing_shows_only_your_own_threads(self, client):
        threads = as_user(client, "alice").get("/api/v1/threads").json()
        assert [t["threadId"] for t in threads] == ["alice-thread"]

    def test_someone_elses_thread_reads_as_missing(self, client):
        assert (
            as_user(client, "bob").get("/api/v1/threads/alice-thread/messages").status_code == 404
        )

    def test_someone_elses_thread_cannot_be_deleted(self, client):
        assert as_user(client, "bob").delete("/api/v1/threads/alice-thread").status_code == 404

    def test_a_failed_delete_leaves_the_thread_intact(self, client):
        as_user(client, "bob").delete("/api/v1/threads/alice-thread")
        assert (
            as_user(client, "alice").get("/api/v1/threads/alice-thread/messages").status_code == 200
        )

    def test_your_own_thread_still_works(self, client):
        messages = as_user(client, "alice").get("/api/v1/threads/alice-thread/messages").json()
        assert messages[0]["content"] == "alice's secret"

    def test_a_missing_thread_and_a_forbidden_one_answer_alike(self, client):
        """Distinguishing them would confirm which thread ids are real."""
        forbidden = as_user(client, "bob").get("/api/v1/threads/alice-thread/messages")
        missing = as_user(client, "bob").get("/api/v1/threads/no-such-thread/messages")
        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.json() == missing.json()


class TestIdentityComesFromTheServer:
    def test_the_body_cannot_choose_a_user(self):
        client, runtime = make_app()
        payload = make_input().model_dump(by_alias=True, mode="json")
        payload["userId"] = "administrator"

        as_user(client, "alice").post("/api/v1/channels/web/agui", json=payload)

        assert runtime.agent.last_kwargs["user_id"] == "alice"

    def test_the_query_is_filtered_by_the_database_not_by_us(self):
        """Pushed down, so a miss cannot be told apart from a nonexistent row.

        Fetching everything and filtering afterwards passes the same assertions
        while leaking existence through timing — and is one forgotten branch
        away from leaking the content.
        """
        db = two_user_db()
        client, _ = make_app(db)

        as_user(client, "alice").get("/api/v1/threads")

        assert db.queried_user_ids == ["alice"]


class TestSingleUserMode:
    def test_no_resolver_means_everything_is_shared(self):
        """A legitimate local-tool configuration, and it must keep working."""
        runtime = AgentRuntime(agent=FakeAgent([run_completed()]), db=two_user_db())
        app = FastAPI()
        app.include_router(make_agui_router(runtime))
        client = TestClient(app)

        assert len(client.get("/api/v1/threads").json()) == 2

    def test_the_absence_of_authentication_is_announced(self, caplog):
        """So that "no auth" is a choice somebody made, not one they missed."""
        runtime = AgentRuntime(agent=FakeAgent([run_completed()]))
        with caplog.at_level("WARNING"):
            make_agui_router(runtime)
        assert "single-user mode" in caplog.text

    def test_a_configured_resolver_says_nothing(self, caplog):
        runtime = AgentRuntime(agent=FakeAgent([run_completed()]))
        with caplog.at_level("WARNING"):
            make_agui_router(runtime, resolve_user_id=lambda _: "alice")
        assert "single-user mode" not in caplog.text


class TestRunIsolation:
    """The long-run routes are GETs carrying only an id, so they need this most."""

    @pytest.fixture
    def wired(self):
        log = InMemoryRunEventLog()
        runtime = AgentRuntime(agent=FakeAgent([run_completed()]), stores=Stores(event_log=log))
        manager = LongRunManager(runtime, log=log)
        client, _ = make_app(long_runs=manager, runtime=runtime)
        return client, manager

    def test_someone_elses_run_cannot_be_aborted(self, wired):
        client, _ = wired
        payload = make_input().model_dump(by_alias=True, mode="json")
        as_user(client, "alice").post("/api/v1/channels/web/agui?detach=1", json=payload)

        assert as_user(client, "bob").post("/api/v1/runs/run-1/abort").status_code == 404

    def test_an_unknown_run_answers_the_same_way(self, wired):
        client, _ = wired
        assert as_user(client, "bob").post("/api/v1/runs/nope/abort").json() == {"aborted": False}

    def test_someone_elses_active_runs_are_invisible(self, wired):
        client, _ = wired
        payload = make_input().model_dump(by_alias=True, mode="json")
        as_user(client, "alice").post("/api/v1/channels/web/agui?detach=1", json=payload)

        assert as_user(client, "bob").get("/api/v1/threads/thread-1/active").json() == []


class TestServiceLevel:
    """The same rules below HTTP, since the service is usable on its own."""

    async def test_the_service_pushes_the_user_into_every_query(self):
        db = two_user_db()
        service = ThreadService(db)

        await service.list_threads(user_id="alice")
        await service.replay_messages("alice-thread", user_id="alice")
        await service.delete_thread("alice-thread", user_id="alice")

        assert db.queried_user_ids == ["alice", "alice", "alice"]

    async def test_deleting_someone_elses_thread_does_nothing(self):
        db = two_user_db()
        service = ThreadService(db)

        result = await service.delete_thread("alice-thread", user_id="bob")

        assert result["ok"] is False
        assert db.deleted == []

    async def test_attaching_to_someone_elses_run_raises(self):
        log = InMemoryRunEventLog()
        await log.start_run("run-1", "t1", user_id="alice")
        manager = LongRunManager(
            AgentRuntime(agent=FakeAgent(), stores=Stores(event_log=log)), log=log
        )

        with pytest.raises(RunNotOwned):
            [frame async for frame in manager.attach("run-1", user_id="bob")]
