"""Unit tests for app/sources/google.py (M2.2).

Mirrors the Brave adapter tests with Google-specific response shape
(items[].link / .title / .snippet, max_num=10, requires both api_key
and cse_id to be configured).
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.sources.google import GOOGLE_MAX_NUM, GOOGLE_SEARCH_URL, GoogleSource
from app.types import BridgeSource


def _ok(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"items": items})


def _make_source(
    api_key: str | None = "k",
    cse_id: str | None = "c",
    handler=None,
):
    transport = httpx.MockTransport(handler) if handler else None
    return GoogleSource(api_key=api_key, cse_id=cse_id, transport=transport)


# ---------- property contracts ----------


def test_property_contracts():
    source = _make_source()
    assert source.name == "google"
    assert source.source_enum == BridgeSource.GOOGLE
    assert source.priority == 30  # last-resort per master plan §4 P1
    assert source.cost_per_query == Decimal("0.005")


def test_is_configured_requires_both_key_and_cse_id():
    assert _make_source(api_key="k", cse_id="c").is_configured() is True
    assert _make_source(api_key=None, cse_id="c").is_configured() is False
    assert _make_source(api_key="k", cse_id=None).is_configured() is False
    assert _make_source(api_key=None, cse_id=None).is_configured() is False
    assert _make_source(api_key="", cse_id="c").is_configured() is False
    assert _make_source(api_key="k", cse_id="").is_configured() is False


# ---------- search behavior ----------


@pytest.mark.asyncio
async def test_unconfigured_search_returns_empty_without_http():
    called: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        called.append(req)
        return httpx.Response(500)

    source = _make_source(api_key=None, cse_id="c", handler=handler)
    results = await source.search("anything")
    assert results == []
    assert called == []


@pytest.mark.asyncio
async def test_happy_path_normalizes_response():
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([
            {"link": "https://a.com/1", "title": "A", "snippet": "snippet a"},
            {"link": "https://a.com/2", "title": "B", "snippet": "snippet b"},
        ])

    source = _make_source(handler=handler)
    results = await source.search("hello")

    assert len(results) == 2
    assert results[0].url == "https://a.com/1"
    assert results[0].title == "A"
    assert results[0].snippet == "snippet a"
    assert results[0].source_rank == 1


@pytest.mark.asyncio
async def test_request_carries_query_key_cse_id_and_num():
    captured: dict[str, httpx.Request] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return _ok([])

    source = _make_source(api_key="my-key", cse_id="my-cse", handler=handler)
    await source.search("austin coffee", max_results=5)

    req = captured["req"]
    assert str(req.url).startswith(GOOGLE_SEARCH_URL)
    assert req.url.params["q"] == "austin coffee"
    assert req.url.params["key"] == "my-key"
    assert req.url.params["cx"] == "my-cse"
    assert req.url.params["num"] == "5"


@pytest.mark.asyncio
async def test_max_results_clamped_to_google_upper_bound():
    """Google Custom Search caps `num` at 10 per call."""
    captured: dict[str, httpx.Request] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return _ok([])

    source = _make_source(handler=handler)
    await source.search("q", max_results=50)
    assert captured["req"].url.params["num"] == str(GOOGLE_MAX_NUM)


@pytest.mark.asyncio
async def test_max_results_floored_at_one():
    captured: dict[str, httpx.Request] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["req"] = req
        return _ok([])

    source = _make_source(handler=handler)
    await source.search("q", max_results=0)
    assert captured["req"].url.params["num"] == "1"


@pytest.mark.asyncio
async def test_empty_items_returns_empty_list():
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([])

    source = _make_source(handler=handler)
    assert await source.search("q") == []


@pytest.mark.asyncio
async def test_missing_items_key_returns_empty_list():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    source = _make_source(handler=handler)
    assert await source.search("q") == []


@pytest.mark.asyncio
async def test_null_items_value_returns_empty_list():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": None})

    source = _make_source(handler=handler)
    assert await source.search("q") == []


@pytest.mark.asyncio
async def test_missing_fields_default_to_empty_strings():
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([{"link": "https://a.com/1"}])  # title + snippet missing

    source = _make_source(handler=handler)
    results = await source.search("q")
    assert len(results) == 1
    assert results[0].url == "https://a.com/1"
    assert results[0].title == ""
    assert results[0].snippet == ""


@pytest.mark.asyncio
async def test_http_error_propagates():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "quota exceeded"})

    source = _make_source(handler=handler)
    with pytest.raises(httpx.HTTPStatusError):
        await source.search("q")


@pytest.mark.asyncio
async def test_response_truncated_to_num():
    """If Google returns more than `num` items (shouldn't, but defensive)."""
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([
            {"link": f"https://a.com/{i}", "title": f"t{i}", "snippet": f"s{i}"}
            for i in range(15)
        ])

    source = _make_source(handler=handler)
    results = await source.search("q", max_results=5)
    assert len(results) == 5
