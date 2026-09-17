"""SerperTools, against a mocked transport (no network, no API key needed)."""

from __future__ import annotations

import json

import httpx
import pytest

from agno_relay.tools.serper import SerperError, SerperTools

SEARCH_RESPONSE = {
    "searchParameters": {"q": "agno", "type": "search"},
    "answerBox": {"answer": "Agno is a Python agent framework."},
    "organic": [
        {
            "title": "Agno docs",
            "link": "https://docs.agno.com",
            "snippet": "Build agents.",
            "position": 1,
            "sitelinks": [{"title": "noise", "link": "https://x"}],
        },
        {
            "title": "Agno on GitHub",
            "link": "https://github.com/agno-agi/agno",
            "snippet": "Source code.",
            "position": 2,
        },
    ],
    "credits": 1,
}


class Recorder:
    """A mock transport that records requests and replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        status, payload = response
        return httpx.Response(status, json=payload)

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def make_tools(responses=None, **kwargs):
    recorder = Recorder(responses or [(200, SEARCH_RESPONSE)])
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    kwargs.setdefault("cache_ttl", 0)
    tools = SerperTools(api_key="test-key", client=client, **kwargs)
    return tools, recorder


class TestOutputFormats:
    async def test_compact_keeps_the_useful_fields_and_drops_the_noise(self):
        tools, _ = make_tools()
        result = json.loads(await tools.search_web("agno"))
        assert result["query"] == "agno"
        assert result["answer"] == "Agno is a Python agent framework."
        assert result["results"][0]["title"] == "Agno docs"
        assert result["results"][0]["link"] == "https://docs.agno.com"
        assert "position" not in result["results"][0]
        assert "sitelinks" not in result["results"][0]

    async def test_compact_is_much_smaller_than_raw(self):
        compact, _ = make_tools(output_format="compact")
        raw, _ = make_tools(output_format="raw")
        assert len(await compact.search_web("agno")) < len(await raw.search_web("agno")) / 1.5

    async def test_markdown_renders_a_numbered_list(self):
        tools, _ = make_tools(output_format="markdown")
        result = await tools.search_web("agno")
        assert "1. **Agno docs**" in result
        assert "https://docs.agno.com" in result
        assert "> Agno is a Python agent framework." in result

    async def test_raw_passes_the_response_through_untouched(self):
        tools, _ = make_tools(output_format="raw")
        assert json.loads(await tools.search_web("agno")) == SEARCH_RESPONSE

    async def test_no_results_is_reported_rather_than_left_blank(self):
        tools, _ = make_tools([(200, {"organic": []})], output_format="markdown")
        assert "No results found." in await tools.search_web("nothing")


class TestEndpoints:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("search_web", "/search"),
            ("search_news", "/news"),
            ("search_images", "/images"),
            ("search_videos", "/videos"),
            ("search_places", "/places"),
            ("search_scholar", "/scholar"),
            ("search_patents", "/patents"),
        ],
    )
    async def test_each_search_hits_its_own_endpoint(self, method, path):
        tools, recorder = make_tools(
            enable_images=True,
            enable_videos=True,
            enable_places=True,
            enable_scholar=True,
            enable_patents=True,
        )
        await getattr(tools, method)("query")
        assert recorder.requests[0].url.path == path

    async def test_scrape_uses_the_scrape_host(self):
        tools, recorder = make_tools([(200, {"markdown": "# Title\n\nBody", "metadata": {}})])
        await tools.scrape_webpage("https://example.com")
        assert recorder.requests[0].url.host == "scrape.serper.dev"
        assert recorder.bodies[0]["url"] == "https://example.com"

    def test_only_the_enabled_tools_are_registered(self):
        tools, _ = make_tools(enable_news=False, enable_scrape=False)
        names = {getattr(f, "__name__", "") for f in tools.tools}
        assert "search_web" in names
        assert "search_news" not in names
        assert "scrape_webpage" not in names

    def test_the_default_tool_set_is_search_news_and_scrape(self):
        tools, _ = make_tools()
        assert {getattr(f, "__name__", "") for f in tools.tools} == {
            "search_web",
            "search_news",
            "scrape_webpage",
        }


class TestRequestParameters:
    async def test_locale_and_result_count_are_sent(self):
        tools, recorder = make_tools(location="jp", language="ja", num_results=5)
        await tools.search_web("agno")
        body = recorder.bodies[0]
        assert body == {"q": "agno", "num": 5, "gl": "jp", "hl": "ja"}

    async def test_a_per_call_result_count_wins(self):
        tools, recorder = make_tools(num_results=10)
        await tools.search_web("agno", num_results=3)
        assert recorder.bodies[0]["num"] == 3

    async def test_the_date_filter_is_forwarded(self):
        tools, recorder = make_tools(date_range="qdr:d")
        await tools.search_web("news")
        assert recorder.bodies[0]["tbs"] == "qdr:d"


class TestCaching:
    async def test_an_identical_query_is_served_from_cache(self):
        tools, recorder = make_tools(cache_ttl=60)
        first = await tools.search_web("agno")
        second = await tools.search_web("agno")
        assert first == second
        assert len(recorder.requests) == 1
        assert tools.usage.cache_hits == 1

    async def test_a_different_query_is_fetched(self):
        tools, recorder = make_tools(cache_ttl=60)
        await tools.search_web("agno")
        await tools.search_web("something else")
        assert len(recorder.requests) == 2

    async def test_clearing_the_cache_forces_a_refetch(self):
        tools, recorder = make_tools(cache_ttl=60)
        await tools.search_web("agno")
        tools.clear_cache()
        await tools.search_web("agno")
        assert len(recorder.requests) == 2

    async def test_caching_can_be_disabled(self):
        tools, recorder = make_tools(cache_ttl=0)
        await tools.search_web("agno")
        await tools.search_web("agno")
        assert len(recorder.requests) == 2


class TestErrorHandling:
    async def test_a_missing_api_key_is_reported_not_raised(self, monkeypatch):
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        tools = SerperTools(api_key=None)
        result = json.loads(await tools.search_web("agno"))
        assert "SERPER_API_KEY" in result["error"]

    async def test_an_empty_query_is_rejected_without_a_request(self):
        tools, recorder = make_tools()
        result = json.loads(await tools.search_web(""))
        assert "error" in result
        assert recorder.requests == []

    async def test_a_client_error_is_not_retried(self):
        tools, recorder = make_tools([(401, {"message": "unauthorized"})])
        result = json.loads(await tools.search_web("agno"))
        assert "401" in result["error"]
        assert len(recorder.requests) == 1

    async def test_a_server_error_is_retried_then_reported(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        tools, recorder = make_tools([(503, {})], max_retries=3)
        result = json.loads(await tools.search_web("agno"))
        assert len(recorder.requests) == 3
        assert "503" in result["error"]
        assert tools.usage.errors == 1

    async def test_a_retry_can_succeed(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        tools, recorder = make_tools([(503, {}), (200, SEARCH_RESPONSE)], max_retries=3)
        result = json.loads(await tools.search_web("agno"))
        assert result["results"][0]["title"] == "Agno docs"
        assert len(recorder.requests) == 2

    async def test_a_timeout_is_retried(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        timeout = httpx.ConnectTimeout("too slow")
        tools, recorder = make_tools([timeout, timeout, (200, SEARCH_RESPONSE)], max_retries=3)
        result = json.loads(await tools.search_web("agno"))
        assert result["results"]
        assert len(recorder.requests) == 3

    async def test_search_many_isolates_a_failing_query(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        tools, _ = make_tools()
        results = await tools.search_many(["a", "b"])
        assert set(results) == {"a", "b"}


class TestUsage:
    async def test_credits_accumulate_across_calls(self):
        tools, _ = make_tools()
        await tools.search_web("one")
        await tools.search_web("two")
        assert tools.usage.credits == 2
        assert tools.usage.requests == 2

    async def test_usage_serializes_for_a_billing_event(self):
        tools, _ = make_tools()
        await tools.search_web("agno")
        assert tools.usage.as_dict() == {
            "credits": 1,
            "requests": 1,
            "cacheHits": 0,
            "errors": 0,
        }


class TestBatch:
    async def test_search_many_runs_every_query(self):
        tools, recorder = make_tools()
        results = await tools.search_many(["one", "two", "three"])
        assert len(results) == 3
        assert len(recorder.requests) == 3

    async def test_concurrency_is_bounded(self):
        tools, _ = make_tools(max_concurrency=2)
        results = await tools.search_many([f"q{i}" for i in range(6)])
        assert len(results) == 6


async def _no_sleep(_seconds):
    return None


def test_serper_error_is_a_runtime_error():
    assert issubclass(SerperError, RuntimeError)
