"""B.3 — per-EII rate limiter tests."""

import pytest

from tests.auth_helpers import sign_test_ept


# ---- tier_for_score ----------------------------------------------------


def test_tier_for_score_anchors():
    from app.eii.tiers import tier_for_score

    assert tier_for_score(950).name == "exceptional"
    assert tier_for_score(900).name == "exceptional"  # inclusive floor
    assert tier_for_score(800).name == "trusted"
    assert tier_for_score(600).name == "developing"
    assert tier_for_score(450).name == "watch"
    assert tier_for_score(300).name == "critical"
    assert tier_for_score(0).name == "critical"
    assert tier_for_score(-50).name == "critical"  # defensive
    assert tier_for_score(1500).name == "exceptional"  # defensive


def test_tier_per_minute_anchors():
    """Master plan calls for these specific anchors."""
    from app.eii.tiers import tier_for_score

    assert tier_for_score(950).requests_per_minute == 200  # exceptional
    assert tier_for_score(0).requests_per_minute == 5      # critical


# ---- /integrity gated endpoint ----------------------------------------


@pytest.mark.asyncio
async def test_integrity_endpoint_returns_tier(gated_client, ept_keypair):
    """Default score 500 → developing tier → 50/min."""
    token = sign_test_ept(ept_keypair, passport="ET26-DEV-AAAA")
    resp = await gated_client.get("/integrity", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["passport"] == "ET26-DEV-AAAA"
    assert data["score"] == 500
    assert data["tier"] == "developing"
    assert data["limit_per_minute"] == 50

    # Headers surface the same data
    assert resp.headers["X-Eternitas-Tier"] == "developing"
    assert resp.headers["X-Eternitas-Score"] == "500"
    assert resp.headers["X-RateLimit-Limit"] == "50"
    assert resp.headers["X-RateLimit-Count"] == "1"


@pytest.mark.asyncio
async def test_integrity_endpoint_high_score(gated_client, ept_keypair):
    """Score 950 → exceptional tier → 200/min."""
    from app.main import app

    app.state.score_cache.scores["ET26-EXC-AAAA"] = 950
    token = sign_test_ept(ept_keypair, passport="ET26-EXC-AAAA")
    resp = await gated_client.get("/integrity", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "exceptional"
    assert data["limit_per_minute"] == 200


@pytest.mark.asyncio
async def test_rate_limit_blocks_after_critical_threshold(gated_client, ept_keypair):
    """Critical tier (5/min) blocks the 6th request inside the window."""
    from app.main import app

    app.state.score_cache.scores["ET26-CRIT-AAAA"] = 100  # critical → 5/min
    token = sign_test_ept(ept_keypair, passport="ET26-CRIT-AAAA")
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(1, 6):
        resp = await gated_client.get("/integrity", headers=headers)
        assert resp.status_code == 200, f"req {i} should pass"
        assert resp.headers["X-RateLimit-Count"] == str(i)

    # 6th request → 429
    resp = await gated_client.get("/integrity", headers=headers)
    assert resp.status_code == 429
    assert resp.headers["X-Eternitas-Tier"] == "critical"
    assert resp.headers["X-RateLimit-Limit"] == "5"
    assert resp.headers["Retry-After"] == "60"
    assert "tier" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_rate_limit_isolated_per_passport(gated_client, ept_keypair):
    """Hitting the limit with one passport doesn't lock out a different one."""
    from app.main import app

    app.state.score_cache.scores["ET26-AAAA-AAAA"] = 100  # critical
    app.state.score_cache.scores["ET26-BBBB-BBBB"] = 100  # critical
    t_a = sign_test_ept(ept_keypair, passport="ET26-AAAA-AAAA")
    t_b = sign_test_ept(ept_keypair, passport="ET26-BBBB-BBBB")

    # Burn passport A's budget (5/min critical)
    for _ in range(5):
        await gated_client.get("/integrity", headers={"Authorization": f"Bearer {t_a}"})
    blocked = await gated_client.get("/integrity", headers={"Authorization": f"Bearer {t_a}"})
    assert blocked.status_code == 429

    # Passport B should still get through
    fresh = await gated_client.get("/integrity", headers={"Authorization": f"Bearer {t_b}"})
    assert fresh.status_code == 200
    assert fresh.headers["X-RateLimit-Count"] == "1"


@pytest.mark.asyncio
async def test_rate_limit_fails_open_when_redis_down(gated_client, ept_keypair):
    """No Redis configured → no enforcement; requests still succeed."""
    from app.main import app

    app.state.redis = None
    app.state.score_cache.scores["ET26-CRIT-AAAA"] = 100
    token = sign_test_ept(ept_keypair, passport="ET26-CRIT-AAAA")
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(20):  # past any tier cap
        resp = await gated_client.get("/integrity", headers=headers)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_score_cache_unit_returns_neutral_on_404():
    """When eternitas returns 404, score cache falls back to neutral."""
    import httpx

    from app.eii.score_cache import NEUTRAL_SCORE, IntegrityScoreCache

    cache = IntegrityScoreCache(eternitas_base_url="https://eternitas.test")

    async def _stub_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Passport not found"})

    transport = httpx.MockTransport(_stub_handler)
    # Monkey-patch the AsyncClient context the helper opens. Cleanest is
    # to just call the inner _fetch with a forged client — but since the
    # public API takes no transport, we stub via httpx's MockTransport
    # globally for this call only.
    import app.eii.score_cache as score_cache_mod

    real_async_client = score_cache_mod.httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(transport=transport, timeout=5.0)

    score_cache_mod.httpx.AsyncClient = patched_async_client
    try:
        score = await cache.get("ET-NOPE-AAAA")
        assert score == NEUTRAL_SCORE
    finally:
        score_cache_mod.httpx.AsyncClient = real_async_client


@pytest.mark.asyncio
async def test_score_cache_ttl_serves_cached_value():
    """Successful fetch is cached; second call hits the cache, not network."""
    import httpx

    from app.eii.score_cache import IntegrityScoreCache

    cache = IntegrityScoreCache(eternitas_base_url="https://eternitas.test", ttl_seconds=60)
    call_count = 0

    async def _stub_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"overall": 750, "passport_kind": "bot"})

    import app.eii.score_cache as score_cache_mod

    real_async_client = score_cache_mod.httpx.AsyncClient
    transport = httpx.MockTransport(_stub_handler)

    def patched_async_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(transport=transport, timeout=5.0)

    score_cache_mod.httpx.AsyncClient = patched_async_client
    try:
        s1 = await cache.get("ET-X")
        s2 = await cache.get("ET-X")
        assert s1 == 750
        assert s2 == 750
        assert call_count == 1, "second call should be cached"
    finally:
        score_cache_mod.httpx.AsyncClient = real_async_client
