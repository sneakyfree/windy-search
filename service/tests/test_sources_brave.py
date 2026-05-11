"""Unit tests for app/sources/brave.py (M2.1).

Uses httpx.MockTransport to avoid real network calls. Covers:

  * Property contract (name, source_enum, priority, cost_per_query)
  * `is_configured()` reflects api_key presence
  * Unconfigured source returns [] without calling the HTTP endpoint
  * Happy-path response shape (Brave's `web.results[]` → RawResult)
  * `max_results` honored and clamped to Brave's 20-result limit
  * Empty/missing web.results in response → []
  * HTTP errors propagate (router's `_safe_search` catches)
  * Request shape: query in `q`, count in `count`, key in header
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.sources.brave import BRAVE_MAX_COUNT, BRAVE_SEARCH_URL, BraveSource
from app.types import BridgeSource


def _ok(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"web": {"results": items}})


def _make_source(api_key: str | None, handler):
    transport = httpx.MockTransport(handler) if handler else None
    return BraveSource(api_key=api_key, transport=transport)


# ---------- property contracts ----------


def test_property_contracts():
    source = BraveSource(api_key="anything")
    assert source.name == "brave"
    assert source.source_enum == BridgeSource.BRAVE
    assert source.priority == 10
    assert source.cost_per_query == Decimal("0.005")


def test_is_configured_when_key_present():
    assert BraveSource(api_key="x").is_configured() is True


def test_is_configured_false_when_key_missing():
    assert BraveSource(api_key=None).is_configured() is False
    assert BraveSource(api_key="").is_configured() is False


# ---------- search behavior ----------


@pytest.mark.asyncio
async def test_unconfigured_search_returns_empty_without_http():
    """No API key → no HTTP call; return []."""
    called = []

    def handler(req: httpx.Request) -> httpx.Response:
        called.append(req)
        return httpx.Response(500)  # would fail if called

    source = _make_source(None, handler)
    results = await source.search("anything")
    assert results == []
    assert called == []  # no HTTP call attempted


@pytest.mark.asyncio
async def test_happy_path_normalizes_response():
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([
            {"url": "https://a.com/1", "title": "A", "description": "snippet a"},
            {"url": "https://a.com/2", "title": "B", "description": "snippet b"},
        ])

    source = _make_source("test-key", handler)
    results = await source.search("hello")

    assert len(results) == 2
    assert results[0].url == "https://a.com/1"
    assert results[0].title == "A"
    assert results[0].snippet == "snippet a"
    assert results[0].source_rank == 1
    assert results[1].source_rank == 2


@pytest.mark.asyncio
async def test_request_carries_query_and_count_and_key():
    captured: dict[str, httpx.Request] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return _ok([])

    source = _make_source("my-secret-key", handler)
    await source.search("austin coffee", max_results=5)

    req = captured["req"]
    assert str(req.url).startswith(BRAVE_SEARCH_URL)
    # Query in `q`
    assert req.url.params["q"] == "austin coffee"
    # Count in `count`
    assert req.url.params["count"] == "5"
    # Key in header
    assert req.headers["X-Subscription-Token"] == "my-secret-key"


@pytest.mark.asyncio
async def test_max_results_clamped_to_brave_upper_bound():
    """Brave's `count` parameter is bounded at 20."""
    captured: dict[str, httpx.Request] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return _ok([])

    source = _make_source("key", handler)
    await source.search("q", max_results=999)
    assert captured["req"].url.params["count"] == str(BRAVE_MAX_COUNT)


@pytest.mark.asyncio
async def test_max_results_floored_at_one():
    captured: dict[str, httpx.Request] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return _ok([])

    source = _make_source("key", handler)
    await source.search("q", max_results=0)
    assert captured["req"].url.params["count"] == "1"


@pytest.mark.asyncio
async def test_response_truncated_to_count():
    """If Brave returns more than `count` items, we trim to `count`."""
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([
            {"url": f"https://a.com/{i}", "title": f"t{i}", "description": f"s{i}"}
            for i in range(15)
        ])

    source = _make_source("key", handler)
    results = await source.search("q", max_results=5)
    assert len(results) == 5


@pytest.mark.asyncio
async def test_empty_results_returns_empty_list():
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([])

    source = _make_source("key", handler)
    results = await source.search("q")
    assert results == []


@pytest.mark.asyncio
async def test_missing_web_key_returns_empty_list():
    """A pathological Brave response missing the `web` key shouldn't crash."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    source = _make_source("key", handler)
    results = await source.search("q")
    assert results == []


@pytest.mark.asyncio
async def test_missing_fields_default_to_empty_strings():
    """Brave occasionally returns partial items (no description, etc.)."""
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([
            {"url": "https://a.com/1"},  # title + description missing
        ])

    source = _make_source("key", handler)
    results = await source.search("q")
    assert len(results) == 1
    assert results[0].url == "https://a.com/1"
    assert results[0].title == ""
    assert results[0].snippet == ""


@pytest.mark.asyncio
async def test_http_error_propagates():
    """Router's `_safe_search` catches; the adapter should not eat the
    error itself (otherwise the router can't log it with the source name)."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad key"})

    source = _make_source("bad-key", handler)
    with pytest.raises(httpx.HTTPStatusError):
        await source.search("q")


@pytest.mark.asyncio
async def test_network_error_propagates():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    source = _make_source("key", handler)
    with pytest.raises(httpx.ConnectError):
        await source.search("q")


@pytest.mark.asyncio
async def test_null_web_results_value():
    """If `web.results` is JSON null rather than missing, still return []."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"web": {"results": None}})

    source = _make_source("key", handler)
    results = await source.search("q")
    assert results == []
