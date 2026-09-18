"""Async Serper search toolkit.

Agno ships a ``SerperTools``, but it blocks the event loop (it uses ``requests``
inside an async agent), returns Serper's raw JSON verbatim, and has no caching.
The raw payload is the real problem: a single ten-result web search is several
thousand tokens of ``position``, ``imageUrl``, ``sitelinks`` and ``attributes``
the model will never read, paid for on every call and again on every subsequent
turn that keeps it in context.

This toolkit normalizes results into a small set of fields and renders them
compactly by default, which is roughly a fifth of the tokens for the same
information. It is fully async, caches identical queries for the length of a
conversation, retries transient failures, bounds concurrency, and tracks the
credits Serper reports so you can bill or budget against real usage.

Setup::

    from agno_harness import SerperTools

    agent = Agent(model=..., tools=[SerperTools(api_key=...)])

``SERPER_API_KEY`` is picked up from the environment when ``api_key`` is omitted.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from agno.tools import Toolkit

SEARCH_URL = "https://google.serper.dev"
SCRAPE_URL = "https://scrape.serper.dev"

OutputFormat = Literal["compact", "markdown", "raw"]

# Endpoints that behave like a search: a query in, a result list out.
_SEARCH_ENDPOINTS = {
    "search": ("organic", "web pages"),
    "news": ("news", "news articles"),
    "images": ("images", "images"),
    "videos": ("videos", "videos"),
    "places": ("places", "places"),
    "maps": ("places", "map results"),
    "scholar": ("organic", "academic papers"),
    "patents": ("organic", "patents"),
}

# Result keys worth keeping, in the order a reader wants them.
_KEEP_KEYS = (
    "title",
    "link",
    "url",
    "snippet",
    "date",
    "source",
    "address",
    "rating",
    "ratingCount",
    "publicationInfo",
    "year",
    "citedBy",
    "duration",
    "channel",
    "imageUrl",
    "priceRange",
    "phoneNumber",
    "website",
)

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class SerperError(RuntimeError):
    """Serper rejected the request or stayed unreachable after retries."""


@dataclass
class SerperUsage:
    """Credits Serper reported, accumulated across calls."""

    credits: int = 0
    requests: int = 0
    cache_hits: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "credits": self.credits,
            "requests": self.requests,
            "cacheHits": self.cache_hits,
            "errors": self.errors,
        }


@dataclass
class _CacheEntry:
    value: dict[str, Any]
    expires_at: float


class _TTLCache:
    """Small LRU with expiry. Identical queries inside one conversation are common."""

    def __init__(self, maxsize: int, ttl_seconds: float) -> None:
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.value

    def put(self, key: str, value: dict[str, Any]) -> None:
        if self.maxsize <= 0:
            return
        self._entries[key] = _CacheEntry(value=value, expires_at=time.monotonic() + self.ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


@dataclass
class SerperResult:
    """One normalized hit."""

    title: str = ""
    link: str = ""
    snippet: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_markdown(self, index: int) -> str:
        head = f"{index}. **{self.title or 'Untitled'}**"
        if self.link:
            head += f" — {self.link}"
        lines = [head]
        if self.snippet:
            lines.append(f"   {self.snippet}")
        detail = ", ".join(f"{k}: {v}" for k, v in self.extra.items() if v not in (None, ""))
        if detail:
            lines.append(f"   ({detail})")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.title:
            out["title"] = self.title
        if self.link:
            out["link"] = self.link
        if self.snippet:
            out["snippet"] = self.snippet
        out.update({k: v for k, v in self.extra.items() if v not in (None, "")})
        return out


class SerperTools(Toolkit):
    """Google search, news, images, videos, places, scholar, patents and scraping.

    Parameters
    ----------
    api_key:
        Serper key. Falls back to ``SERPER_API_KEY``.
    location / language:
        Serper's ``gl`` and ``hl``. Defaults to US English.
    num_results:
        Default result count per search.
    date_range:
        Serper's ``tbs`` filter, e.g. ``"qdr:d"`` for the past day.
    output_format:
        ``compact`` (default) trims each hit to the fields a model actually
        reads and returns JSON. ``markdown`` renders a numbered list, which
        smaller models follow more reliably. ``raw`` passes Serper's response
        through untouched — use it when you are post-processing yourself.
    cache_ttl / cache_size:
        Repeat-query cache. Set ``cache_ttl=0`` to disable.
    max_concurrency:
        Ceiling on in-flight requests, so a parallel fan-out cannot trip rate
        limits.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        location: str = "us",
        language: str = "en",
        num_results: int = 10,
        date_range: str | None = None,
        output_format: OutputFormat = "compact",
        timeout: float = 30.0,
        max_retries: int = 3,
        cache_ttl: float = 300.0,
        cache_size: int = 256,
        max_concurrency: int = 5,
        enable_search: bool = True,
        enable_news: bool = True,
        enable_images: bool = False,
        enable_videos: bool = False,
        enable_places: bool = False,
        enable_scholar: bool = False,
        enable_patents: bool = False,
        enable_scrape: bool = True,
        client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> None:
        self.api_key = api_key or os.getenv("SERPER_API_KEY")
        self.location = location
        self.language = language
        self.num_results = num_results
        self.date_range = date_range
        self.output_format: OutputFormat = output_format
        self.max_retries = max_retries
        self.usage = SerperUsage()

        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._cache = _TTLCache(maxsize=cache_size, ttl_seconds=cache_ttl)
        self._semaphore = asyncio.Semaphore(max_concurrency)

        tools: list[Any] = []
        if enable_search:
            tools.append(self.search_web)
        if enable_news:
            tools.append(self.search_news)
        if enable_images:
            tools.append(self.search_images)
        if enable_videos:
            tools.append(self.search_videos)
        if enable_places:
            tools.append(self.search_places)
        if enable_scholar:
            tools.append(self.search_scholar)
        if enable_patents:
            tools.append(self.search_patents)
        if enable_scrape:
            tools.append(self.scrape_webpage)

        super().__init__(name="serper_tools", tools=tools, **kwargs)

    # ── agent-facing tools ────────────────────────────────────────────────

    async def search_web(self, query: str, num_results: int | None = None) -> str:
        """Search Google for the query.

        Args:
            query: What to search for.
            num_results: How many results to return.

        Returns:
            The search results.
        """
        return await self._search("search", query, num_results)

    async def search_news(self, query: str, num_results: int | None = None) -> str:
        """Search Google News for recent articles about the query.

        Args:
            query: What to search for.
            num_results: How many articles to return.

        Returns:
            The news results.
        """
        return await self._search("news", query, num_results)

    async def search_images(self, query: str, num_results: int | None = None) -> str:
        """Search Google Images for the query.

        Args:
            query: What to search for.
            num_results: How many images to return.

        Returns:
            The image results.
        """
        return await self._search("images", query, num_results)

    async def search_videos(self, query: str, num_results: int | None = None) -> str:
        """Search Google Videos for the query.

        Args:
            query: What to search for.
            num_results: How many videos to return.

        Returns:
            The video results.
        """
        return await self._search("videos", query, num_results)

    async def search_places(self, query: str, num_results: int | None = None) -> str:
        """Search Google Maps for places matching the query.

        Args:
            query: What to search for, e.g. "coffee near Shibuya".
            num_results: How many places to return.

        Returns:
            The place results.
        """
        return await self._search("places", query, num_results)

    async def search_scholar(self, query: str, num_results: int | None = None) -> str:
        """Search Google Scholar for academic papers about the query.

        Args:
            query: What to search for.
            num_results: How many papers to return.

        Returns:
            The scholar results.
        """
        return await self._search("scholar", query, num_results)

    async def search_patents(self, query: str, num_results: int | None = None) -> str:
        """Search Google Patents for patents matching the query.

        Args:
            query: What to search for.
            num_results: How many patents to return.

        Returns:
            The patent results.
        """
        return await self._search("patents", query, num_results)

    async def scrape_webpage(self, url: str, markdown: bool = True) -> str:
        """Fetch a webpage and extract its readable content.

        Args:
            url: The page to fetch.
            markdown: Return markdown instead of plain text.

        Returns:
            The page content.
        """
        if not url:
            return _error("Please provide a URL to scrape")
        try:
            payload = await self._request(
                "scrape", {"url": url, "includeMarkdown": markdown}, cacheable=True
            )
        except SerperError as exc:
            return _error(str(exc))

        if self.output_format == "raw":
            return json.dumps(payload, ensure_ascii=False)
        text = payload.get("markdown") or payload.get("text") or ""
        metadata = payload.get("metadata") or {}
        title = metadata.get("title") or payload.get("title") or ""
        if self.output_format == "markdown":
            return f"# {title}\n\n{text}".strip() if title else text
        return json.dumps({"url": url, "title": title, "content": text}, ensure_ascii=False)

    # ── batch helper (not exposed to the model) ───────────────────────────

    async def search_many(
        self,
        queries: list[str],
        *,
        endpoint: str = "search",
        num_results: int | None = None,
    ) -> dict[str, str]:
        """Run several searches concurrently, bounded by ``max_concurrency``.

        For your own orchestration code — a research agent fanning out over
        sub-questions, say. It is not registered as a tool because models handle
        one query at a time far more reliably.
        """
        results = await asyncio.gather(
            *(self._search(endpoint, q, num_results) for q in queries),
            return_exceptions=True,
        )
        return {
            query: (_error(str(result)) if isinstance(result, BaseException) else result)
            for query, result in zip(queries, results, strict=True)
        }

    # ── plumbing ──────────────────────────────────────────────────────────

    async def _search(self, endpoint: str, query: str, num_results: int | None) -> str:
        if not query:
            return _error("Please provide a query to search for")
        params: dict[str, Any] = {"q": query, "num": num_results or self.num_results}
        try:
            payload = await self._request(endpoint, params, cacheable=True)
        except SerperError as exc:
            return _error(str(exc))
        return self._render(endpoint, query, payload)

    def _render(self, endpoint: str, query: str, payload: dict[str, Any]) -> str:
        if self.output_format == "raw":
            return json.dumps(payload, ensure_ascii=False)

        result_key, label = _SEARCH_ENDPOINTS.get(endpoint, ("organic", "results"))
        results = [_normalize(item) for item in (payload.get(result_key) or [])]
        answer = _answer_box(payload)

        if self.output_format == "markdown":
            lines = [f"Search results for **{query}** ({label}):"]
            if answer:
                lines.append(f"\n> {answer}")
            if not results:
                lines.append("\nNo results found.")
            for index, result in enumerate(results, start=1):
                lines.append(result.to_markdown(index))
            return "\n".join(lines)

        compact: dict[str, Any] = {"query": query, "results": [r.to_dict() for r in results]}
        if answer:
            compact["answer"] = answer
        return json.dumps(compact, ensure_ascii=False)

    async def _request(
        self, endpoint: str, params: dict[str, Any], *, cacheable: bool
    ) -> dict[str, Any]:
        if not self.api_key:
            raise SerperError("No Serper API key. Pass api_key=... or set SERPER_API_KEY.")

        body = dict(params)
        if endpoint != "scrape":
            if self.location:
                body.setdefault("gl", self.location)
            if self.language:
                body.setdefault("hl", self.language)
            if self.date_range:
                body.setdefault("tbs", self.date_range)

        cache_key = f"{endpoint}:{json.dumps(body, sort_keys=True, ensure_ascii=False)}"
        if cacheable:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self.usage.cache_hits += 1
                return cached

        url = SCRAPE_URL if endpoint == "scrape" else f"{SEARCH_URL}/{endpoint}"
        payload = await self._post_with_retries(url, body)

        self.usage.requests += 1
        credits = payload.get("credits")
        if isinstance(credits, int):
            self.usage.credits += credits

        if cacheable:
            self._cache.put(cache_key, payload)
        return payload

    async def _post_with_retries(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"X-API-KEY": self.api_key or "", "Content-Type": "application/json"}
        last_error: str = "unknown error"

        for attempt in range(self.max_retries):
            try:
                async with self._semaphore:
                    client = await self._get_client()
                    response = await client.post(url, headers=headers, json=body)
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    # 4xx other than rate limiting is a bad request; retrying
                    # will not fix it and only burns time.
                    self.usage.errors += 1
                    raise SerperError(f"HTTP {response.status_code}: {response.text[:200]}")
                else:
                    data = response.json()
                    return data if isinstance(data, dict) else {"data": data}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self.max_retries - 1:
                # Jittered backoff so a fan-out does not retry in lockstep.
                await asyncio.sleep((2**attempt) * 0.5 * (0.5 + random.random()))

        self.usage.errors += 1
        raise SerperError(f"Serper request failed after {self.max_retries} attempts: {last_error}")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client, if this toolkit created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def clear_cache(self) -> None:
        self._cache.clear()


def _normalize(item: Any) -> SerperResult:
    """Keep the fields a reader uses; drop ranking noise and tracking metadata."""
    if not isinstance(item, dict):
        return SerperResult(title=str(item))
    extra = {
        key: item[key]
        for key in _KEEP_KEYS
        if key in item and key not in ("title", "link", "snippet", "url")
    }
    return SerperResult(
        title=str(item.get("title") or ""),
        link=str(item.get("link") or item.get("url") or ""),
        snippet=str(item.get("snippet") or item.get("description") or ""),
        extra=extra,
    )


def _answer_box(payload: dict[str, Any]) -> str:
    """Serper's direct answer, when Google produced one."""
    for key in ("answerBox", "knowledgeGraph"):
        box = payload.get(key)
        if not isinstance(box, dict):
            continue
        for field_name in ("answer", "snippet", "description", "title"):
            value = box.get(field_name)
            if value:
                return str(value)
    return ""


def _error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


__all__ = ["SerperError", "SerperResult", "SerperTools", "SerperUsage"]
