"""B.10 — cross-tenant result cache tests."""

import pytest

from tests.auth_helpers import sign_test_ept
from tests.test_web_search import RecordingEternitasClient

# ---- result_cache primitives ----------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_returns_none():
    from app.eii.result_cache import get_cached
    from tests.conftest import FakeRedisB3

    redis = FakeRedisB3()
    result = await get_cached(redis, "web.search", {"query": "anything"})
    assert result is None


@pytest.mark.asyncio
async def test_cache_set_then_get_round_trip():
    from app.eii.result_cache import get_cached, set_cached
    from tests.conftest import FakeRedisB3

    redis = FakeRedisB3()
    payload = {"query": "windy ecosystem", "limit": 5}
    value = {"results": [{"url": "u", "title": "t"}], "backend": "brave"}

    await set_cached(redis, "web.search", payload, value)
    got = await get_cached(redis, "web.search", payload)
    assert got == value


@pytest.mark.asyncio
async def test_cache_namespaces_per_capability():
    """Same input under different capabilities → separate cache entries."""
    from app.eii.result_cache import get_cached, set_cached
    from tests.conftest import FakeRedisB3

    redis = FakeRedisB3()
    payload = {"x": 1}

    await set_cached(redis, "web.search", payload, {"r": "search-result"})
    await set_cached(redis, "web.fetch", payload, {"r": "fetch-result"})

    s = await get_cached(redis, "web.search", payload)
    f = await get_cached(redis, "web.fetch", payload)
    assert s == {"r": "search-result"}
    assert f == {"r": "fetch-result"}


@pytest.mark.asyncio
async def test_cache_fails_open_when_redis_none():
    from app.eii.result_cache import get_cached, set_cached

    # Both must no-op without raising
    assert await get_cached(None, "web.search", {"q": "x"}) is None
    await set_cached(None, "web.search", {"q": "x"}, {"r": "y"})


@pytest.mark.asyncio
async def test_cost_refund_decrements_counter():
    """B.9.refund unwinds a charge so cap reflects real spend."""
    from app.eii.cost_cap import _key, charge, refund
    from tests.conftest import FakeRedisB3

    redis = FakeRedisB3()
    await charge(redis, "ET-X", "web.search", cap_usd=5.0, warning_pct=0.8)
    after_charge = redis._strings[_key("ET-X")]
    assert after_charge == 500

    await refund(redis, "ET-X", "web.search")
    after_refund = redis._strings[_key("ET-X")]
    assert after_refund == 0


# ---- /web/search × cache --------------------------------------------


def _patch_search_backend(monkeypatch, results=None, backend="brave"):
    from app.web.search import SearchResponse, SearchResult

    results = results or [{"url": "u", "title": "t", "snippet": "s"}]
    call_count = {"n": 0}

    async def fake_search(query, limit, *, brave_api_key, timeout_seconds=8.0):
        call_count["n"] += 1
        return SearchResponse(
            results=[SearchResult(**r) for r in results],
            backend=backend,
            query=query,
        )

    monkeypatch.setattr("app.web.router.search", fake_search)
    return call_count


@pytest.mark.asyncio
async def test_search_first_call_populates_cache_second_call_hits(
    gated_client, ept_keypair, monkeypatch
):
    """Second identical request short-circuits the backend."""
    from app.main import app

    app.state.eternitas_client = RecordingEternitasClient()
    call_count = _patch_search_backend(monkeypatch)

    token1 = sign_test_ept(ept_keypair, passport="ET26-CACH-AAAA")
    token2 = sign_test_ept(ept_keypair, passport="ET26-CACH-BBBB")  # different agent

    body = {"query": "windy", "limit": 5}
    h1 = {"Authorization": f"Bearer {token1}"}
    h2 = {"Authorization": f"Bearer {token2}"}
    r1 = await gated_client.post("/web/search", headers=h1, json=body)
    r2 = await gated_client.post("/web/search", headers=h2, json=body)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["cache_hit"] is False
    assert r2.json()["cache_hit"] is True
    assert r2.json()["results"] == r1.json()["results"]
    # Backend called exactly once across the two callers
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_search_cache_hit_refunds_cost(gated_client, ept_keypair, monkeypatch):
    from app.eii.cost_cap import _key
    from app.main import app

    app.state.eternitas_client = RecordingEternitasClient()
    _patch_search_backend(monkeypatch)

    token = sign_test_ept(ept_keypair, passport="ET26-RFND-AAAA")
    headers = {"Authorization": f"Bearer {token}"}

    # First call: populates cache + charges 500 microcents
    await gated_client.post("/web/search", headers=headers, json={"query": "q"})
    after_first = app.state.redis._strings[_key("ET26-RFND-AAAA")]
    assert after_first == 500

    # Second call: cache hit + refund → counter unchanged from first call
    await gated_client.post("/web/search", headers=headers, json={"query": "q"})
    after_second = app.state.redis._strings[_key("ET26-RFND-AAAA")]
    assert after_second == 500  # not 1000


@pytest.mark.asyncio
async def test_search_different_queries_dont_share_cache(
    gated_client, ept_keypair, monkeypatch
):
    from app.main import app

    app.state.eternitas_client = RecordingEternitasClient()
    call_count = _patch_search_backend(monkeypatch)

    token = sign_test_ept(ept_keypair, passport="ET26-DIFQ-AAAA")
    headers = {"Authorization": f"Bearer {token}"}

    await gated_client.post("/web/search", headers=headers, json={"query": "first"})
    await gated_client.post("/web/search", headers=headers, json={"query": "second"})

    assert call_count["n"] == 2  # neither cached against the other


# ---- /web/fetch × cache ---------------------------------------------


@pytest.mark.asyncio
async def test_fetch_cache_serves_pagination_from_one_entry(
    gated_client, ept_keypair, monkeypatch
):
    """Different (offset, max_chars) for the same URL share one cache entry."""
    from app.main import app
    from app.web.fetch import FetchResponse

    app.state.eternitas_client = RecordingEternitasClient()
    call_count = {"n": 0}

    async def fake_fetch(url, *, max_chars, offset, **kwargs):
        call_count["n"] += 1
        full = "ABCDEFGHIJ" * 100  # 1000 chars
        # Mirror real fetch: when called with full window, returns the
        # full body in `content` (the route re-slices).
        return FetchResponse(
            final_url=url,
            status_code=200,
            content_type="text/plain",
            content=full,
            total_chars=len(full),
            offset=0,
            max_chars=max_chars,
            truncated=False,
        )

    monkeypatch.setattr("app.web.router.fetch_url", fake_fetch)

    token = sign_test_ept(ept_keypair, passport="ET26-PAGN-AAAA")
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await gated_client.post(
        "/web/fetch", headers=headers,
        json={"url": "https://x.test/", "offset": 0, "max_chars": 100},
    )
    r2 = await gated_client.post(
        "/web/fetch", headers=headers,
        json={"url": "https://x.test/", "offset": 500, "max_chars": 100},
    )

    assert r1.json()["cache_hit"] is False
    assert r2.json()["cache_hit"] is True
    assert r1.json()["content"][:10] == "ABCDEFGHIJ"
    assert r2.json()["content"][:10] == "ABCDEFGHIJ"  # 500 % 10 == 0, so same prefix
    assert r2.json()["offset"] == 500
    assert r2.json()["max_chars"] == 100
    assert call_count["n"] == 1  # backend hit only the first time
