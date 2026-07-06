"""Smoke tests for the B.1 scaffold — health + readiness."""

import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    """Liveness probe always 200 with service identity."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "windy-search"
    assert data["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_health_ready_no_redis(client):
    """Without Redis configured, readiness reports ready (B.1 doesn't
    need Redis; B.3+ will tighten this). The bare test client never runs
    lifespan, so install a router with one configured source — readiness
    requires it since 2026-07-06 (prod ran with zero configured bridges
    while claiming "ready").
    """
    from app.main import app
    from app.router import Router
    from app.sources.stubs import StubOwnCorpusSource

    app.state.search_router = Router([StubOwnCorpusSource()])
    try:
        resp = await client.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["sources_configured"] >= 1
    finally:
        app.state.search_router = None


@pytest.mark.asyncio
async def test_health_ready_degraded_without_sources(client):
    """Zero configured sources ⇒ degraded, even with Redis happy. This is
    the exact prod condition found live 2026-07-06 (no bridge keys set)."""
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["sources_configured"] == 0


@pytest.mark.asyncio
async def test_openapi_includes_service_identity(client):
    """OpenAPI spec is reachable and self-describing."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["info"]["title"] == "Windy Search"
    assert "/health" in spec["paths"]
    assert "/health/ready" in spec["paths"]


@pytest.mark.asyncio
async def test_webhooks_stub_accepts_and_discards(client):
    """POST /webhooks → 204, no auth, no body required. Lets the eternitas
    dispatcher's per-platform failure counter stay at 0 until we wire a
    real consumer."""
    resp = await client.post("/webhooks", json={"event_type": "test", "data": {}})
    assert resp.status_code == 204
