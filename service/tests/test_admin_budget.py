"""Per-passport budget-cap override (Windy Admin Phase 3).

Contract: the override changes the CAP a passport is judged against
(not its spend), persists across months, and is set/cleared through
the thin admin API — which is disabled without a configured token.
"""

import pytest

from app.eii.cost_cap import (
    MICROCENTS_PER_USD,
    charge,
    get_cap_override,
    set_cap_override,
)
from tests.conftest import FakeRedisB3


@pytest.mark.asyncio
async def test_override_changes_cap_judgement():
    redis = FakeRedisB3()
    # Default $0.0005 cap: one search fills it, second denied.
    await charge(redis, "ET-OVR", "web.search", cap_usd=0.0005, warning_pct=0.8)
    denied = await charge(redis, "ET-OVR", "web.search", cap_usd=0.0005, warning_pct=0.8)
    assert denied.allowed is False

    # Raise the cap to $1 via override — same passport can charge again.
    await set_cap_override(redis, "ET-OVR", int(1.0 * MICROCENTS_PER_USD))
    allowed = await charge(redis, "ET-OVR", "web.search", cap_usd=0.0005, warning_pct=0.8)
    assert allowed.allowed is True
    assert allowed.cap_microcents == MICROCENTS_PER_USD

    # Clearing the override restores the caller-supplied cap.
    await set_cap_override(redis, "ET-OVR", None)
    again = await charge(redis, "ET-OVR", "web.search", cap_usd=0.0005, warning_pct=0.8)
    assert again.allowed is False
    assert await get_cap_override(redis, "ET-OVR") is None


@pytest.mark.asyncio
async def test_override_can_lower_cap():
    redis = FakeRedisB3()
    await set_cap_override(redis, "ET-LOW", 100)  # $0.0001 — below one search
    decision = await charge(redis, "ET-LOW", "web.search", cap_usd=5.0, warning_pct=0.8)
    # First charge is allowed (pre-charge state 0 < 100)…
    assert decision.allowed is True
    # …but the next is denied even though the default cap is $5.
    denied = await charge(redis, "ET-LOW", "web.search", cap_usd=5.0, warning_pct=0.8)
    assert denied.allowed is False


@pytest.mark.asyncio
async def test_admin_api_disabled_without_token(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.config import get_settings
    from app.main import create_app

    # Patch the CACHED settings instance (suite convention) — clearing the
    # module cache strands app-captured settings and flakes other tests.
    monkeypatch.setattr(get_settings(), "admin_api_token", None)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.put(
            "/admin/budget-cap/ET-X",
            json={"cap_usd": 1.0},
            headers={"Authorization": "Bearer anything"},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_admin_api_token_gate(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "admin_api_token", "wsat_test")
    app = create_app()
    app.state.redis = FakeRedisB3()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        bad = await client.put(
            "/admin/budget-cap/ET-X",
            json={"cap_usd": 1.0},
            headers={"Authorization": "Bearer nope"},
        )
        assert bad.status_code == 401

        ok = await client.put(
            "/admin/budget-cap/ET-X",
            json={"cap_usd": 15.0},
            headers={"Authorization": "Bearer wsat_test"},
        )
        assert ok.status_code == 200
        assert ok.json() == {"passport": "ET-X", "cap_usd": 15.0, "override": True}

        read = await client.get(
            "/admin/budget-cap/ET-X", headers={"Authorization": "Bearer wsat_test"}
        )
        assert read.json()["cap_usd"] == 15.0

        cleared = await client.put(
            "/admin/budget-cap/ET-X",
            json={"cap_usd": None},
            headers={"Authorization": "Bearer wsat_test"},
        )
        assert cleared.json()["override"] is False
