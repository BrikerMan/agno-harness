"""StreamUI inside a run: validation, resolvers, and the tool-side emit.

The parser tests cover the wire format. These cover what the runtime adds on top
of it — the two things a pure text parser cannot do, because both need the
catalog and one of them needs to await.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from agno_relay import AguiRuntime, SequencerMode
from agno_relay.core.streamui import BlockSchema, CardCatalog, ItemSchema
from agno_relay.runtime.modules.streamui import emit_item, emit_text, ui_block

from .conftest import FakeAgent, collect, content, customs, make_input, run_completed, text_of


class MovieRef(ItemSchema):
    schema_name = "movie-card-item"
    id: int


class MovieList(BlockSchema):
    """A row of film cards."""

    schema_name = "movie-list"
    item = MovieRef
    title: str | None = None


class Steps(BlockSchema):
    schema_name = "steps"


class Step(ItemSchema):
    schema_name = "step"
    label: str
    state: Literal["running", "done", "failed"] = "running"


class LogCard(BlockSchema):
    schema_name = "log"
    body = "text"


def make_catalog() -> CardCatalog:
    catalog = CardCatalog([MovieList, Steps, LogCard])
    catalog.register_item(Step)
    return catalog


def runtime_for(chunks, *, catalog=None, **kwargs) -> AguiRuntime:
    return AguiRuntime(
        agent=FakeAgent(chunks),
        catalog=catalog if catalog is not None else make_catalog(),
        sequencer_mode=SequencerMode.AUDIT,
        **kwargs,
    )


def fence(*lines: str) -> str:
    return "".join(lines)


class TestValidation:
    async def test_a_valid_item_passes_through(self):
        runtime = runtime_for(
            [
                content(
                    '```stream-ui {"schema": "movie-list", "title": "Sci-fi"}\n{"id": 603}\n```\n'
                ),
                run_completed(),
            ]
        )
        events = await collect(runtime.stream_events(make_input()))
        item = customs(events, "ui.item")[0]
        assert item.value["data"] == {"id": 603}
        assert "error" not in item.value

    async def test_a_card_survives_a_stray_newline_of_reasoning_mid_fence(self):
        """Thinking models drip a lone ``"\\n"`` of reasoning between chunks.

        It used to split the answer into two text messages, and a fence opened
        in one and closed in the other parsed as neither: the user saw a bare
        ``` and no card.
        """
        runtime = runtime_for(
            [
                content('```stream-ui {"schema": "movie-list"}\n'),
                content("", reasoning="\n"),
                content('{"id": 603}\n```\n'),
                run_completed(),
            ]
        )
        events = await collect(runtime.stream_events(make_input()))
        assert customs(events, "ui.item")[0].value["data"] == {"id": 603}
        assert customs(events, "ui.block.end")
        assert text_of(events) == ""

    async def test_an_invalid_item_becomes_an_error_without_killing_the_block(self):
        runtime = runtime_for(
            [
                content(
                    '```stream-ui {"schema": "movie-list"}\n{"id": "not a number"}\n{"id": 2}\n```\n'
                ),
                run_completed(),
            ]
        )
        events = await collect(runtime.stream_events(make_input()))
        items = customs(events, "ui.item")
        assert "id" in items[0].value["error"]
        assert items[1].value["data"] == {"id": 2}
        assert customs(events, "ui.block.end")

    async def test_invalid_header_props_mark_the_block(self):
        runtime = runtime_for(
            [
                content('```stream-ui {"schema": "movie-list", "titel": "typo"}\n```\n'),
                run_completed(),
            ]
        )
        events = await collect(runtime.stream_events(make_input()))
        assert "titel" in customs(events, "ui.block.start")[0].value["error"]

    async def test_without_a_catalog_nothing_is_validated(self):
        """A usable prototyping mode, and the reason the catalog is optional."""
        runtime = AguiRuntime(
            agent=FakeAgent(
                [
                    content('```stream-ui {"schema": "whatever"}\n{"free": "form"}\n```\n'),
                    run_completed(),
                ]
            ),
            sequencer_mode=SequencerMode.AUDIT,
        )
        events = await collect(runtime.stream_events(make_input()))
        assert customs(events, "ui.item")[0].value["data"] == {"free": "form"}


class TestResolvers:
    """The model chooses; the server supplies the facts."""

    async def test_a_resolver_enriches_the_item(self):
        catalog = make_catalog()

        @catalog.resolver("movie-card-item")
        async def resolve(data):
            return {"title": "Alien", "rating": 8.4}

        runtime = runtime_for(
            [content('```stream-ui {"schema": "movie-list"}\n{"id": 348}\n```\n'), run_completed()],
            catalog=catalog,
        )
        events = await collect(runtime.stream_events(make_input()))
        item = customs(events, "ui.item")[0]
        assert item.value["data"] == {"id": 348}
        assert item.value["resolved"] == {"title": "Alien", "rating": 8.4}

    async def test_the_resolver_sees_the_validated_data(self):
        catalog = make_catalog()
        seen = []

        @catalog.resolver("movie-card-item")
        async def resolve(data):
            seen.append(data)
            return {}

        runtime = runtime_for(
            [
                content('```stream-ui {"schema": "movie-list"}\n{"id": "348"}\n```\n'),
                run_completed(),
            ],
            catalog=catalog,
        )
        await collect(runtime.stream_events(make_input()))
        assert seen == [{"id": 348}]

    async def test_a_failing_resolver_degrades_the_card_rather_than_dropping_it(self):
        catalog = make_catalog()

        @catalog.resolver("movie-card-item")
        async def resolve(data):
            raise RuntimeError("tmdb is down")

        runtime = runtime_for(
            [content('```stream-ui {"schema": "movie-list"}\n{"id": 1}\n```\n'), run_completed()],
            catalog=catalog,
        )
        events = await collect(runtime.stream_events(make_input()))
        item = customs(events, "ui.item")[0]
        assert item.value["data"] == {"id": 1}
        assert "tmdb is down" in item.value["resolveError"]
        assert "resolved" not in item.value

    async def test_items_resolve_one_at_a_time_as_they_arrive(self):
        """Batching would trade progressive rendering for a saving nobody needs:
        items arrive at the speed the model writes them, slower than any query."""
        catalog = make_catalog()
        order = []

        @catalog.resolver("movie-card-item")
        async def resolve(data):
            order.append(data["id"])
            return {"seq": len(order)}

        runtime = runtime_for(
            [
                content('```stream-ui {"schema": "movie-list"}\n{"id": 1}\n'),
                content('{"id": 2}\n```\n'),
                run_completed(),
            ],
            catalog=catalog,
        )
        events = await collect(runtime.stream_events(make_input()))
        assert order == [1, 2]
        assert [i.value["resolved"]["seq"] for i in customs(events, "ui.item")] == [1, 2]

    async def test_resolvers_can_be_replaced_wholesale_for_tests(self):
        """A golden trace cannot depend on a live API, and this is the seam."""
        catalog = make_catalog()

        async def stub(data):
            return {"title": "stubbed"}

        catalog.replace_resolvers({"movie-card-item": stub})
        runtime = runtime_for(
            [content('```stream-ui {"schema": "movie-list"}\n{"id": 1}\n```\n'), run_completed()],
            catalog=catalog,
        )
        events = await collect(runtime.stream_events(make_input()))
        assert customs(events, "ui.item")[0].value["resolved"] == {"title": "stubbed"}


class TestToolSideEmit:
    """Some cards the model never asks for: progress, results before the summary."""

    async def test_a_tool_can_open_a_block_and_append_items(self):
        async def parent(**kwargs):
            async with ui_block("steps"):
                await emit_item("step", {"label": "searching"})
                await emit_item("step", {"label": "ranking", "state": "done"})
            yield content("Found two.")
            yield run_completed()

        runtime = runtime_for([])
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = await collect(runtime.stream_events(make_input()))

        assert customs(events, "ui.block.start")[0].value["schema"] == "steps"
        labels = [i.value["data"]["label"] for i in customs(events, "ui.item")]
        assert labels == ["searching", "ranking"]
        assert customs(events, "ui.block.end")[0].value["total"] == 2

    async def test_a_tool_block_is_validated_like_a_fenced_one(self):
        async def parent(**kwargs):
            async with ui_block("steps"):
                await emit_item("step", {"label": "x", "state": "nonsense"})
            yield run_completed()

        runtime = runtime_for([])
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = await collect(runtime.stream_events(make_input()))
        assert "state" in customs(events, "ui.item")[0].value["error"]

    async def test_a_tool_can_stream_raw_text(self):
        async def parent(**kwargs):
            async with ui_block("log"):
                await emit_text("line one\n")
                await emit_text("line two\n")
            yield run_completed()

        runtime = runtime_for([])
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = await collect(runtime.stream_events(make_input()))
        assert [e.value["delta"] for e in customs(events, "ui.text")] == [
            "line one\n",
            "line two\n",
        ]

    async def test_the_block_closes_even_when_the_tool_raises(self):
        async def parent(**kwargs):
            async with ui_block("steps"):
                await emit_item("step", {"label": "x"})
                raise RuntimeError("tool exploded")
            yield run_completed()  # pragma: no cover

        runtime = runtime_for([])
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = await collect(runtime.stream_events(make_input()))
        assert customs(events, "ui.block.end")

    async def test_outside_a_run_the_helpers_are_a_no_op(self):
        """So the same tool works unchanged in a test or a script."""
        async with ui_block("steps") as block:
            await emit_item("step", {"label": "x"})
            await emit_text("ignored")
        assert block.count == 1

    async def test_a_tool_block_never_appears_in_the_visible_text(self):
        async def parent(**kwargs):
            async with ui_block("steps"):
                await emit_item("step", {"label": "x"})
            yield content("All done.")
            yield run_completed()

        runtime = runtime_for([])
        runtime.agent.arun = lambda **kwargs: parent(**kwargs)
        events = await collect(runtime.stream_events(make_input()))
        assert text_of(events) == "All done."

    async def test_artifact_persistence_with_template_root_dir(self, tmp_path):
        class DeckCard(BlockSchema):
            schema_name = "deck"
            body = "text"
            emit_text = False
            filepath: str

            @classmethod
            def parse_line(cls, line: str, block: Any) -> Mapping[str, Any] | None:
                if line.startswith("<!-- SLIDE:"):
                    return {"page": int(line.split(":")[1].replace("-->", "").strip())}
                return None

        catalog = CardCatalog([DeckCard])
        root_dir = str(tmp_path / "artifacts" / "{task-id}" / "output")
        runtime = runtime_for(
            [
                content('```stream-ui {"schema": "deck", "filepath": "presentation.html"}\n'),
                content("<!-- SLIDE: 1 -->\n<h1>Slide 1</h1>\n"),
                content("<!-- SLIDE: 2 -->\n<h1>Slide 2</h1>\n"),
                content("```\n"),
                run_completed(),
            ],
            catalog=catalog,
            artifact_root_dir=root_dir,
        )

        inp = make_input()
        events = await collect(runtime.stream_events(inp))

        # emit_text=False means NO ui.text events
        assert customs(events, "ui.text") == []

        # parse_line emitted ui.item events
        items = customs(events, "ui.item")
        assert len(items) == 2
        assert items[0].value["data"] == {"page": 1}
        assert items[1].value["data"] == {"page": 2}

        # Block end contains savedPath
        end_events = customs(events, "ui.block.end")
        assert len(end_events) == 1
        end_val = end_events[0].value
        expected_file = tmp_path / "artifacts" / inp.thread_id / "output" / "presentation.html"
        assert end_val["savedPath"] == str(expected_file)
        assert end_val["relativePath"] == "presentation.html"
        assert expected_file.exists()
        assert "<!-- SLIDE: 1 -->" in expected_file.read_text(encoding="utf-8")
        assert "<!-- SLIDE: 2 -->" in expected_file.read_text(encoding="utf-8")

    async def test_artifact_persistence_prevents_directory_traversal(self, tmp_path):
        class InsecureCard(BlockSchema):
            schema_name = "insecure-doc"
            body = "text"
            filepath: str

        catalog = CardCatalog([InsecureCard])
        root_dir = str(tmp_path / "safe-dir")
        runtime = runtime_for(
            [
                content(
                    '```stream-ui {"schema": "insecure-doc", "filepath": "../../etc/malicious.txt"}\n'
                ),
                content("pwned\n"),
                content("```\n"),
                run_completed(),
            ],
            catalog=catalog,
            artifact_root_dir=root_dir,
        )

        events = await collect(runtime.stream_events(make_input()))
        end_events = customs(events, "ui.block.end")
        assert len(end_events) == 1
        assert "escapes artifact_root_dir" in end_events[0].value.get("persistenceError", "")
        # File must not be created outside
        assert not (tmp_path / "etc" / "malicious.txt").exists()
