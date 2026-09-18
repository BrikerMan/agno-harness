"""The chunk-to-event trace file."""

from __future__ import annotations

import json

import pytest

from agno_harness import AgentRuntime
from agno_harness.runtime.tracing import TRACE_DIR_ENV, RunTracer

from .conftest import FakeAgent, content, make_input, run_completed, tool_started


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def maps(records):
    return [r for r in records if r["kind"] == "map"]


class TestRunTracer:
    def test_it_is_off_unless_a_directory_is_configured(self, monkeypatch):
        monkeypatch.delenv(TRACE_DIR_ENV, raising=False)
        assert RunTracer.open(None, thread_id="t", run_id="r") is None

    def test_the_environment_variable_turns_it_on(self, monkeypatch, tmp_path):
        monkeypatch.setenv(TRACE_DIR_ENV, str(tmp_path))
        tracer = RunTracer.open(None, thread_id="t", run_id="r")

        assert tracer is not None
        tracer.finish()
        assert (tmp_path / "r.jsonl").exists()

    def test_a_run_id_with_path_separators_cannot_escape_the_directory(self, tmp_path):
        tracer = RunTracer.open(tmp_path, thread_id="t", run_id="../../etc/passwd")

        assert tracer is not None
        tracer.finish()
        assert tracer.path.parent == tmp_path

    def test_each_record_lands_before_the_next_one_opens(self, tmp_path):
        """Written as the run happens, so a crash still leaves a usable trace."""
        tracer = RunTracer.open(tmp_path, thread_id="t", run_id="r")
        assert tracer is not None
        tracer.stage("first")
        tracer.stage("second")

        assert [r["source"] for r in maps(read(tracer.path))] == ["first"]


class TestRuntimeTracing:
    @pytest.fixture
    def trace(self, tmp_path):
        async def run(chunks):
            runtime = AgentRuntime(agent=FakeAgent(chunks), trace_dir=tmp_path)
            async for _ in runtime.stream_events(make_input()):
                pass
            return read(tmp_path / "run-1.jsonl")

        return run

    async def test_every_chunk_is_mapped_to_the_events_it_produced(self, trace):
        records = maps(await trace([content("Hello.")]))

        agno = {r["chunk"]["event"]: r["summary"] for r in records if r["source"] == "agno"}
        assert "RunContent" in agno
        assert "TEXT_MESSAGE_CONTENT" in agno["RunContent"]

    async def test_a_chunk_that_produced_nothing_is_still_recorded(self, trace):
        """The point of the file: see what was swallowed, not only what shipped."""
        records = maps(await trace([content("")]))

        swallowed = [r for r in records if r["source"] == "agno" and not r["events"]]
        assert swallowed, "a chunk yielding no events should still appear"
        assert swallowed[0]["summary"].endswith("-> (nothing)")

    async def test_the_chunk_payload_is_kept_for_writing_parsers_against(self, trace):
        records = maps(await trace([tool_started("c1", "search", {"q": "hi"})]))

        [record] = [r for r in records if r.get("chunk", {}).get("event") == "ToolCallStarted"]
        assert record["chunk"]["tool"]["tool_name"] == "search"

    async def test_bridge_stages_are_labelled_separately_from_agno(self, trace):
        records = maps(await trace([content("Hi.")]))

        sources = {r["source"] for r in records}
        assert "run_started" in sources
        assert "completion" in sources
        assert "agno" in sources

    async def test_offsets_increase_and_gaps_are_never_negative(self, trace):
        records = maps(await trace([content("Hi."), run_completed()]))

        assert [r["t"] for r in records] == sorted(r["t"] for r in records)
        assert all(r["dt"] >= 0 for r in records)

    async def test_the_file_opens_with_the_run_and_closes_with_a_summary(self, trace):
        records = await trace([content("Hi.")])

        assert records[0]["kind"] == "run"
        assert records[0]["runId"] == "run-1"
        summary = records[-1]
        assert summary["kind"] == "summary"
        assert summary["eventCounts"]["TEXT_MESSAGE_CONTENT"] == 1
