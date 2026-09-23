"""ExaTools, against a mocked transport (no network)."""

from __future__ import annotations

import json

import httpx

from agno_harness.tools.exa import ExaTools

HIT = {
    "title": "Agno docs",
    "url": "https://docs.agno.com",
    "text": "Build agents.",
    "score": 0.99,
    "id": "drop-me",
    "publishedDate": "2026-01-01",
}


def _payload(body: dict | None = None, *, status: int = 200, session: str = "") -> httpx.Response:
    headers = {"mcp-session-id": session} if session else {}
    if body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json=body, headers=headers)


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content) if request.content else {}
        method = body.get("method")
        if method == "initialize":
            return _payload(
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"protocolVersion": "2024-11-05"},
                },
                session="sess-1",
            )
        if method == "notifications/initialized":
            return _payload(status=202)
        if method == "tools/call":
            text = json.dumps({"results": [HIT]})
            return _payload(
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            )
        return _payload({"jsonrpc": "2.0", "error": {"message": "unknown method"}}, status=400)


def make_tools(**kwargs) -> tuple[ExaTools, Recorder]:
    recorder = Recorder()
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    tools = ExaTools(client=client, cache_ttl=300, **kwargs)
    return tools, recorder


async def test_search_web_keeps_title_url_and_snippet():
    tools, recorder = make_tools()
    result = await tools.search_web("agno")
    assert "Agno docs" in result
    assert "https://docs.agno.com" in result
    assert "Build agents." in result
    assert "drop-me" not in result
    assert "0.99" not in result
    assert "publishedDate" not in result
    call = json.loads(recorder.requests[-1].content)
    assert call["method"] == "tools/call"
    assert call["params"]["name"] == "web_search_exa"
    assert call["params"]["arguments"] == {"query": "agno", "numResults": 5}
    assert recorder.requests[-1].headers["mcp-session-id"] == "sess-1"


async def test_identical_query_is_cached():
    tools, recorder = make_tools()
    first = await tools.search_web("agno")
    second = await tools.search_web("agno")
    assert first == second
    assert len(recorder.requests) == 3


async def test_http_failure_is_an_english_message():
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    tools = ExaTools(client=client, cache_ttl=0)
    result = await tools.search_web("agno")
    assert result.startswith("Web search failed:")
