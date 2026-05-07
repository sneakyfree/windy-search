"""B.9 — per-passport monthly cost cap tests."""

import pytest

from tests.auth_helpers import sign_test_ept
from tests.test_web_search import RecordingEternitasClient


# ---- cost catalog + charge() unit tests ------------------------------


def test_cost_catalog_contains_known_capabilities():
    from app.eii.cost_cap import COSTS

    assert COSTS["web.search"] == 500     # $0.005
    assert COSTS["web.fetch"] == 1        # $0.000001
    assert COSTS["web.browse"] == 50_000  # $0.05
    assert COSTS["web.extract"] == 20_000 # $0.02


@pytest.mark.asyncio
async def test_charge_under_cap_succeeds():
    from tests.conftest import FakeRedisB3
    from app.eii.cost_cap import charge

    redis = FakeRedisB3()
    decision = await charge(redis, "ET-X", "web.search", cap_usd=5.0, warning_pct=0.8)
    assert decision.allowed is True
    assert decision.used_before == 0
    assert decision.used_after == 500
    assert decision.cost_charged == 500
    assert decision.warning is False


@pytest.mark.asyncio
async def test_charge_accumulates_within_month():
    from tests.conftest import FakeRedisB3
    from app.eii.cost_cap import charge

    redis = FakeRedisB3()
    for _ in range(5):
        await charge(redis, "ET-X", "web.search", cap_usd=5.0, warning_pct=0.8)

    decision = await charge(redis, "ET-X", "web.search", cap_usd=5.0, warning_pct=0.8)
    assert decision.used_before == 5 * 500
    assert decision.used_after == 6 * 500


@pytest.mark.asyncio
async def test_charge_blocks_over_cap_and_rolls_back():
    from tests.conftest import FakeRedisB3
    from app.eii.cost_cap import MICROCENTS_PER_USD, charge

    redis = FakeRedisB3()
    # Cap = $0.001 = 1000 microcents → fits 2 web.search (1000 microcents)
    cap_usd = 0.001

    d1 = await charge(redis, "ET-X", "web.search", cap_usd=cap_usd, warning_pct=0.8)
    assert d1.allowed is True

    d2 = await charge(redis, "ET-X", "web.search", cap_usd=cap_usd, warning_pct=0.8)
    assert d2.allowed is True
    assert d2.used_after == 1000  # exactly at cap

    d3 = await charge(redis, "ET-X", "web.search", cap_usd=cap_usd, warning_pct=0.8)
    assert d3.allowed is False
    assert d3.cost_charged == 0
    # Rolled back so used_after stays at 1000, not bumped past cap
    assert d3.used_after == 1000


@pytest.mark.asyncio
async def test_charge_warning_at_threshold():
    from tests.conftest import FakeRedisB3
    from app.eii.cost_cap import charge

    redis = FakeRedisB3()
    # Cap = 1000 microcents, warning at 80% = 800. web.search costs 500.
    # First call: 0→500, no warning (under threshold)
    # Second call: 500→1000, warning fires (crosses 800)
    cap_usd = 0.001

    d1 = await charge(redis, "ET-X", "web.search", cap_usd=cap_usd, warning_pct=0.8)
    assert d1.warning is False

    d2 = await charge(redis, "ET-X", "web.search", cap_usd=cap_usd, warning_pct=0.8)
    assert d2.warning is True


@pytest.mark.asyncio
async def test_charge_fails_open_when_redis_none():
    from app.eii.cost_cap import charge

    decision = await charge(None, "ET-X", "web.search", cap_usd=5.0, warning_pct=0.8)
    assert decision.allowed is True
    assert decision.cost_charged == 500


# ---- /web/search × cost cap -----------------------------------------


def _patch_search_backend(monkeypatch, results=None, backend="brave"):
    from app.web.search import SearchResponse, SearchResult

    results = results or [{"url": "u", "title": "t", "snippet": "s"}]

    async def fake_search(query, limit, *, brave_api_key, timeout_seconds=8.0):
        return SearchResponse(
            results=[SearchResult(**r) for r in results],
            backend=backend,
            query=query,
        )

    monkeypatch.setattr("app.web.router.search", fake_search)


@pytest.mark.asyncio
async def test_search_response_has_cost_headers(gated_client, ept_keypair, monkeypatch):
    from app.main import app

    app.state.eternitas_client = RecordingEternitasClient()
    _patch_search_backend(monkeypatch)

    token = sign_test_ept(ept_keypair, passport="ET26-COST-AAAA")
    resp = await gated_client.post(
        "/web/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "test"},
    )
    assert resp.status_code == 200
    assert resp.headers["X-Cost-Capability"] == "web.search"
    # Cap from settings default ($5.00)
    assert resp.headers["X-Cost-Cap-USD"] == "5.00"
    # web.search costs 500 microcents = $0.000500
    assert resp.headers["X-Cost-Used-USD"].startswith("0.000500")


@pytest.mark.asyncio
async def test_search_blocks_when_budget_exhausted(
    gated_client, ept_keypair, monkeypatch
):
    """Manually inflate the cost counter past the cap, then verify a fresh
    search gets 429 with the budget detail."""
    from app.main import app
    from app.eii.cost_cap import _key, MICROCENTS_PER_USD

    app.state.eternitas_client = RecordingEternitasClient()
    _patch_search_backend(monkeypatch)

    # 5 USD = 5,000,000 microcents — pre-charge to exactly at cap
    passport = "ET26-EXHA-AAAA"
    redis = app.state.redis
    redis._strings[_key(passport)] = 5 * MICROCENTS_PER_USD

    token = sign_test_ept(ept_keypair, passport=passport)
    resp = await gated_client.post(
        "/web/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "test"},
    )
    assert resp.status_code == 429
    assert "budget" in resp.json()["detail"].lower()
    assert resp.headers["Retry-After"] == "86400"
    assert resp.headers["X-Cost-Capability"] == "web.search"


@pytest.mark.asyncio
async def test_fetch_charged_against_same_budget(
    gated_client, ept_keypair, monkeypatch
):
    """Both /web/search and /web/fetch debit the same monthly counter."""
    from app.main import app
    from app.eii.cost_cap import _key

    app.state.eternitas_client = RecordingEternitasClient()
    _patch_search_backend(monkeypatch)

    from app.web.fetch import FetchResponse

    async def fake_fetch(url, *, max_chars, offset, **kwargs):
        return FetchResponse(
            final_url=url, status_code=200, content_type="text/plain",
            content="ok", total_chars=2, offset=0, max_chars=max_chars, truncated=False,
        )
    monkeypatch.setattr("app.web.router.fetch_url", fake_fetch)

    passport = "ET26-MIX-AAAA"
    token = sign_test_ept(ept_keypair, passport=passport)
    headers = {"Authorization": f"Bearer {token}"}

    s = await gated_client.post("/web/search", headers=headers, json={"query": "q"})
    f = await gated_client.post("/web/fetch", headers=headers, json={"url": "https://x.test/"})
    assert s.status_code == 200
    assert f.status_code == 200

    # search cost (500) + fetch cost (1) = 501 microcents accumulated
    final = app.state.redis._strings[_key(passport)]
    assert final == 501
