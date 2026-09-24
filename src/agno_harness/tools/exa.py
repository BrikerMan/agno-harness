"""Web search through the public Exa MCP. No API key.

The free endpoint is the same one skilless uses: ``https://mcp.exa.ai/mcp``,
tool ``web_search_exa``. The reply is trimmed to title, URL, and snippet so a
raw search payload does not stay in the conversation.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import httpx
from agno.tools import Toolkit

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
_ACCEPT = "application/json, text/event-stream"
_PROTOCOL = "2024-11-05"


@dataclass
class _CacheEntry:
    value: str
    expires_at: float


class _TTLCache:
    def __init__(self, maxsize: int, ttl_seconds: float) -> None:
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()

    def get(self, key: str) -> str | None:
        if self.ttl <= 0:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.value

    def put(self, key: str, value: str) -> None:
        if self.ttl <= 0 or self.maxsize <= 0:
            return
        self._entries[key] = _CacheEntry(value=value, expires_at=time.monotonic() + self.ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)


class ExaTools(Toolkit):
    """Semantic web search. Works without an API key."""

    def __init__(
        self,
        *,
        num_results: int = 5,
        timeout: float = 30.0,
        cache_ttl: float = 300.0,
        cache_size: int = 128,
        client: httpx.AsyncClient | None = None,
        url: str = EXA_MCP_URL,
    ) -> None:
        self.num_results = num_results
        self._timeout = timeout
        self._url = url
        self._client = client
        self._cache = _TTLCache(maxsize=cache_size, ttl_seconds=cache_ttl)
        self._session_id = ""
        self._rpc_id = 0
        super().__init__(name="exa", tools=[self.search_web])

    async def search_web(self, query: str, num_results: int | None = None) -> str:
        """Search the web and return a short list of title, URL, and snippet.

        Args:
            query: What to search for.
            num_results: How many results to return. Defaults to 5.

        Returns:
            A short list of results, or why the search failed.
        """
        text = (query or "").strip()
        if not text:
            return "Web search needs a query."
        count = _clamp(num_results if num_results is not None else self.num_results)
        key = f"{text}\n{count}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            rendered = await self._search(text, count)
        except Exception as exc:
            return f"Web search failed: {_short(exc)}"
        if not rendered.startswith("Web search failed:"):
            self._cache.put(key, rendered)
        return rendered

    async def _search(self, query: str, count: int) -> str:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            self._client = client
        if not self._session_id:
            await self._initialize(client)
        payload = await self._rpc(
            client,
            "tools/call",
            {"name": "web_search_exa", "arguments": {"query": query, "numResults": count}},
        )
        if "error" in payload:
            message = payload["error"].get("message") if isinstance(payload["error"], dict) else ""
            return (
                f"Web search failed: {_short(message or 'the search service rejected the request')}"
            )
        return _render(query, _hits(payload))

    async def _initialize(self, client: httpx.AsyncClient) -> None:
        payload = await self._rpc(
            client,
            "initialize",
            {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "agno-harness", "version": "0.2.1"},
            },
        )
        if "error" in payload:
            raise RuntimeError("could not start a search session")
        await self._notify(client, "notifications/initialized")

    async def _rpc(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self._rpc_id += 1
        body = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        response = await client.post(self._url, headers=self._headers(), json=body)
        session = response.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        return _decode_response(response)

    async def _notify(self, client: httpx.AsyncClient, method: str) -> None:
        response = await client.post(
            self._url,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": method},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": _ACCEPT, "Content-Type": "application/json"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers


def _clamp(value: int) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 5
    return max(1, min(count, 10))


def _short(exc: object) -> str:
    text = str(exc).strip() or "the search service did not respond"
    return text[:200]


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    text = response.text.strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise RuntimeError("the search service returned an unexpected payload")
        return data
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and ("result" in data or "error" in data):
            return data
    raise RuntimeError("the search service returned an unreadable payload")


def _hits(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    content = result.get("content")
    if not isinstance(content, list):
        return []
    hits: list[tuple[str, str, str]] = []
    for block in content:
        if isinstance(block, dict) and block.get("text"):
            hits.extend(_hits_from_text(str(block["text"])))
    return hits


def _hits_from_text(text: str) -> list[tuple[str, str, str]]:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            return _hits_from_json(data)
    return _hits_from_labeled(stripped)


def _hits_from_json(data: Any) -> list[tuple[str, str, str]]:
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        found = next(
            (
                data[key]
                for key in ("results", "organic", "data")
                if isinstance(data.get(key), list)
            ),
            None,
        )
        items = found if found is not None else [data]
    else:
        return []
    hits: list[tuple[str, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        snippet = str(item.get("snippet") or item.get("text") or item.get("summary") or "").strip()
        if len(snippet) > 400:
            snippet = snippet[:400].rstrip() + "..."
        if title or url:
            hits.append((title, url, snippet))
    return hits


def _hits_from_labeled(text: str) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    title = ""
    url = ""
    snippet_lines: list[str] = []

    def flush() -> None:
        nonlocal title, url, snippet_lines
        snippet = " ".join(snippet_lines).strip()
        if title or url:
            hits.append((title, url, snippet))
        title = ""
        url = ""
        snippet_lines = []

    for raw in text.splitlines():
        line = raw.strip()
        lower = line.lower()
        if lower.startswith("title:"):
            if title or url:
                flush()
            title = line.split(":", 1)[1].strip()
        elif lower.startswith("url:"):
            url = line.split(":", 1)[1].strip()
        elif lower.startswith(("text:", "snippet:")):
            snippet_lines.append(line.split(":", 1)[1].strip())
        elif snippet_lines and line:
            snippet_lines.append(line)
    flush()
    return hits


def _render(query: str, hits: list[tuple[str, str, str]]) -> str:
    if not hits:
        return f"No web results for {query}."
    lines = [f"Web results for {query}:"]
    for index, (title, url, snippet) in enumerate(hits, start=1):
        head = f"{index}. **{title or 'Untitled'}**"
        if url:
            head += f" — {url}"
        lines.append(head)
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)
