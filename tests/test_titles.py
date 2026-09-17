"""Thread titles: persist on the session, emit CUSTOM thread.title."""

from __future__ import annotations

from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui.core.types import UserMessage

from agno_relay import AguiRuntime, SequencerMode, make_thread_title_hook
from agno_relay.runtime.titles import EVENT_THREAD_TITLE

from .conftest import FakeAgent, content, customs, make_input, run_completed, types_of
from .test_replay import FakeDb, FakeInput, FakeRun, FakeSession
from .test_runtime import stream


class FakeTitleModel:
    def __init__(self, title: str = "Shipping checklist"):
        self.title = title
        self.prompts: list[str] = []

    async def aresponse(self, messages: list[Any], **_: Any) -> Any:
        self.prompts.append(getattr(messages[0], "content", "") if messages else "")
        return type("Response", (), {"content": self.title})()


def _runtime(*, title: str = "Shipping checklist", db: FakeDb | None = None) -> AguiRuntime:
    agent = FakeAgent([content("Use a checklist."), run_completed("Use a checklist.")])
    agent.model = FakeTitleModel(title)
    agent.db = db
    runtime = AguiRuntime(agent=agent, db=db, sequencer_mode=SequencerMode.AUDIT)
    runtime.on_post_run(make_thread_title_hook(runtime))
    return runtime


class TestGenerateThreadTitle:
    async def test_it_asks_the_model_and_persists_the_name(self):
        session = FakeSession("thread-1", runs=[FakeRun("r1", FakeInput("How do I ship?"), "…")])
        db = FakeDb([session])
        runtime = _runtime(db=db)

        title = await runtime.generate_thread_title("thread-1", user_text="How do I ship?")

        assert title == "Shipping checklist"
        assert session.session_data == {"session_name": "Shipping checklist"}
        prompt = runtime.agent.model.prompts[0]
        assert "How do I ship?" in prompt
        assert "same language as the user" in prompt

    async def test_a_model_failure_returns_none(self):
        class Boom:
            async def aresponse(self, *_a: Any, **_k: Any) -> Any:
                raise RuntimeError("upstream 503")

        agent = FakeAgent()
        agent.model = Boom()
        runtime = AguiRuntime(agent=agent, db=FakeDb([]), sequencer_mode=SequencerMode.AUDIT)
        assert await runtime.generate_thread_title("t1", user_text="hi") is None


class TestThreadTitleHook:
    async def test_the_first_successful_run_emits_thread_title(self):
        db = FakeDb([FakeSession("thread-1")])
        events = await stream(_runtime(db=db), make_input("How do I ship?"))
        titled = customs(events, EVENT_THREAD_TITLE)
        assert titled[0].value == {"threadId": "thread-1", "title": "Shipping checklist"}
        types = types_of(events)
        assert types.index("CUSTOM") < types.index("RUN_FINISHED") or types[-1] == "RUN_FINISHED"
        assert types.index("CUSTOM") < types.index("RUN_FINISHED")

    async def test_a_saved_title_is_not_regenerated(self):
        session = FakeSession("thread-1", session_data={"session_name": "Already named"})
        db = FakeDb([session])
        events = await stream(_runtime(db=db, title="New name"), make_input("second turn"))
        assert customs(events, EVENT_THREAD_TITLE) == []
        assert session.session_data == {"session_name": "Already named"}

    async def test_refresh_title_forces_a_new_name(self):
        session = FakeSession(
            "thread-1",
            runs=[FakeRun("r1", FakeInput("How do I ship?"), "Use a checklist.")],
            session_data={"session_name": "Already named"},
        )
        db = FakeDb([session])
        payload = RunAgentInput(
            thread_id="thread-1",
            run_id="run-1",
            state=None,
            messages=[UserMessage(id="m1", role="user", content="and tests?")],
            tools=[],
            context=[],
            forwarded_props={"refreshTitle": True},
        )
        events = await stream(_runtime(db=db, title="Ship and test"), payload)
        titled = customs(events, EVENT_THREAD_TITLE)
        assert titled[0].value["title"] == "Ship and test"
        assert session.session_data["session_name"] == "Ship and test"

    async def test_a_failed_run_does_not_name_the_thread(self):
        agent = FakeAgent(raise_at=0)
        agent.model = FakeTitleModel("Should not appear")
        runtime = AguiRuntime(agent=agent, db=FakeDb([]), sequencer_mode=SequencerMode.AUDIT)
        runtime.on_post_run(make_thread_title_hook(runtime))
        events = await stream(runtime)
        assert customs(events, EVENT_THREAD_TITLE) == []
        assert types_of(events)[-1] == "RUN_ERROR"

    async def test_a_title_failure_does_not_fail_the_run(self):
        class Boom:
            async def aresponse(self, *_a: Any, **_k: Any) -> Any:
                raise RuntimeError("naming exploded")

        agent = FakeAgent([content("ok"), run_completed("ok")])
        agent.model = Boom()
        runtime = AguiRuntime(agent=agent, db=FakeDb([]), sequencer_mode=SequencerMode.AUDIT)
        runtime.on_post_run(make_thread_title_hook(runtime))
        events = await stream(runtime)
        assert types_of(events)[-1] == "RUN_FINISHED"
        assert customs(events, EVENT_THREAD_TITLE) == []

    async def test_pre_hook_and_post_hook_concurrent_generation(self):
        db = FakeDb([FakeSession("thread-concurrent")])
        agent = FakeAgent([content("answer"), run_completed("answer")])
        agent.model = FakeTitleModel("Concurrent Title")
        agent.db = db
        runtime = AguiRuntime(agent=agent, db=db, sequencer_mode=SequencerMode.AUDIT)
        from agno_relay.runtime.titles import make_thread_title_pre_hook

        runtime.on_pre_run(make_thread_title_pre_hook(runtime))
        runtime.on_post_run(make_thread_title_hook(runtime))

        events = await stream(
            runtime, make_input("How to optimize?", thread_id="thread-concurrent")
        )
        titled = customs(events, EVENT_THREAD_TITLE)
        assert len(titled) == 1
        assert titled[0].value == {"threadId": "thread-concurrent", "title": "Concurrent Title"}
        assert types_of(events)[-1] == "RUN_FINISHED"

    async def test_slow_title_does_not_block_run_finished(self):
        import asyncio

        class SlowTitleModel:
            async def aresponse(self, *_a: Any, **_k: Any) -> Any:
                await asyncio.sleep(0.3)
                return type("Response", (), {"content": "Slow Title"})()

        session = FakeSession("thread-slow")
        db = FakeDb([session])
        agent = FakeAgent([content("quick reply"), run_completed("quick reply")])
        agent.model = SlowTitleModel()
        agent.db = db
        runtime = AguiRuntime(agent=agent, db=db, sequencer_mode=SequencerMode.AUDIT)
        # Use very small timeout so post_hook doesn't block the stream
        runtime.on_post_run(make_thread_title_hook(runtime, timeout=0.01))

        events = await stream(runtime, make_input("Hello", thread_id="thread-slow"))
        # Run completes immediately without blocking
        assert types_of(events)[-1] == "RUN_FINISHED"
        # Wait for the background task to complete and persist
        await asyncio.sleep(0.35)
        assert session.session_data.get("session_name") == "Slow Title"
