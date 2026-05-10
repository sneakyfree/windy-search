"""Search backends for Phase B.4 — Brave (preferred) + DuckDuckGo (fallback).

The two-tier choice mirrors the existing windy-agent direct integration
(`windy-agent/src/windyfly/tools/web_search.py:42-100`) so the wire-level
behavior is familiar to the agent runtime that B.12 will switch over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class SearchResponse:
    results: list[SearchResult]
    backend: str  # "brave" | "ddg"
    query: str


async def search(
    query: str,
    limit: int,
    *,
    brave_api_key: str | None,
    timeout_seconds: float = 8.0,
) -> SearchResponse:
    """Try Brave first when an API key is configured; fall back to DDG."""
    if brave_api_key:
        try:
            results = await _brave_search(query, limit, brave_api_key, timeout_seconds)
            return SearchResponse(results=results, backend="brave", query=query)
        except Exception as e:
            logger.warning("Brave search failed, falling back to DDG: %s", e)

    results = await _ddg_search(query, limit, timeout_seconds)
    return SearchResponse(results=results, backend="ddg", query=query)


async def _brave_search(
    query: str,
    limit: int,
    api_key: str,
    timeout_seconds: float,
) -> list[SearchResult]:
    url = "https://api.search.brave.com/res/v1/web/search"
    params = {"q": query, "count": min(max(limit, 1), 20)}
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    web = data.get("web", {}).get("results", [])
    return [
        SearchResult(
            url=item.get("url", ""),
            title=item.get("title", ""),
            snippet=item.get("description", ""),
        )
        for item in web[:limit]
    ]


async def _ddg_search(
    query: str,
    limit: int,
    timeout_seconds: float,
) -> list[SearchResult]:
    """DuckDuckGo's instant-answer endpoint. Free, no key, but lower
    quality and limited result types — fine as a fallback."""
    url = "https://api.duckduckgo.com/"
    params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    related = data.get("RelatedTopics", [])

    results: list[SearchResult] = []
    for topic in related[:limit]:
        if "Topics" in topic:  # nested category — skip; we want concrete topics
            continue
        results.append(
            SearchResult(
                url=topic.get("FirstURL", ""),
                title=topic.get("Text", "").split(" - ")[0] if topic.get("Text") else "",
                snippet=topic.get("Text", ""),
            )
        )
    return results
