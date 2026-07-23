"""Tests for revocation enforcement — CRL cache + webhook blacklist.

Covers the P0 closed 2026-07-09: a revoked passport's EPT (365-day
offline bearer) must be rejected within one CRL TTL window, and the
gate must fail CLOSED when the CRL is unreachable past max_stale.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from app.auth.revocation import RevocationCache
from tests.auth_helpers import sign_test_ept


def _cache(**kwargs) -> RevocationCache:
    defaults = dict(
        crl_url="https://crl.test/.well-known/eternitas-crl",
        ttl_seconds=30,
        max_stale_seconds=300,
        fail_closed=True,
    )
    defaults.update(kwargs)
    return RevocationCache(**defaults)


def _prime(cache: RevocationCache, revoked: set[str], age_seconds: float = 0.0) -> None:
    """Inject CRL state as if a fetch succeeded `age_seconds` ago."""
    cache._revoked = frozenset(revoked)
    cache._fetched_at = time.time() - age_seconds


class _FetchFails:
    async def __call__(self):
        raise RuntimeError("eternitas unreachable")


# ---- CRL consult ------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_passport_rejected_from_fresh_crl():
    cache = _cache()
    _prime(cache, {"ET26-REVK-AAAA"})
    with pytest.raises(HTTPException) as exc:
        await cache.check("ET26-REVK-AAAA")
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_clear_passport_passes():
    cache = _cache()
    _prime(cache, {"ET26-REVK-AAAA"})
    await cache.check("ET26-GOOD-BBBB")  # no raise


@pytest.mark.asyncio
async def test_stale_crl_triggers_refresh(monkeypatch):
    cache = _cache(ttl_seconds=30)
    _prime(cache, set(), age_seconds=31)  # past TTL

    async def _fetch():
        return frozenset({"ET26-REVK-CCCC"})

    monkeypatch.setattr(cache, "_fetch", _fetch)
    with pytest.raises(HTTPException) as exc:
        await cache.check("ET26-REVK-CCCC")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_fresh_crl_not_refetched(monkeypatch):
    cache = _cache(ttl_seconds=30)
    _prime(cache, set(), age_seconds=1)

    async def _fetch():
        raise AssertionError("must not refetch a fresh CRL")

    monkeypatch.setattr(cache, "_fetch", _fetch)
    await cache.check("ET26-GOOD-DDDD")  # no raise, no fetch


# ---- failure semantics ------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_within_max_stale_serves_stale(monkeypatch):
    cache = _cache(ttl_seconds=30, max_stale_seconds=300)
    _prime(cache, {"ET26-REVK-EEEE"}, age_seconds=60)  # stale but within grace
    monkeypatch.setattr(cache, "_fetch", _FetchFails())

    # Stale CRL still enforced
    with pytest.raises(HTTPException) as exc:
        await cache.check("ET26-REVK-EEEE")
    assert exc.value.status_code == 401
    # Clear passports still pass on stale-but-in-grace data
    await cache.check("ET26-GOOD-FFFF")


@pytest.mark.asyncio
async def test_unreachable_past_max_stale_fails_closed(monkeypatch):
    cache = _cache(ttl_seconds=30, max_stale_seconds=300, fail_closed=True)
    _prime(cache, set(), age_seconds=301)
    monkeypatch.setattr(cache, "_fetch", _FetchFails())

    with pytest.raises(HTTPException) as exc:
        await cache.check("ET26-GOOD-GGGG")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_never_fetched_and_unreachable_fails_closed(monkeypatch):
    cache = _cache(fail_closed=True)  # cold boot, eternitas down
    monkeypatch.setattr(cache, "_fetch", _FetchFails())

    with pytest.raises(HTTPException) as exc:
        await cache.check("ET26-GOOD-HHHH")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_unreachable_past_max_stale_fail_open_posture(monkeypatch):
    cache = _cache(fail_closed=False)
    monkeypatch.setattr(cache, "_fetch", _FetchFails())
    await cache.check("ET26-GOOD-IIII")  # dev posture: no raise


# ---- webhook blacklist ------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_blacklist_rejects_without_crl_fetch(monkeypatch):
    cache = _cache()

    async def _fetch():
        raise AssertionError("blacklist check must not need the CRL")

    monkeypatch.setattr(cache, "_fetch", _fetch)
    cache.blacklist("ET26-REVK-JJJJ")
    with pytest.raises(HTTPException) as exc:
        await cache.check("ET26-REVK-JJJJ")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_suspended_blacklist_expires():
    cache = _cache(suspended_ttl_seconds=3600)
    _prime(cache, set())
    cache.blacklist("ET26-SUSP-KKKK", suspended=True)

    with pytest.raises(HTTPException) as exc:
        await cache.check("ET26-SUSP-KKKK")
    assert exc.value.status_code == 401
    assert "suspended" in exc.value.detail.lower()

    # Simulate the suspension TTL elapsing → entry drops, passport clears
    cache._webhook_suspended["ET26-SUSP-KKKK"] = time.time() - 3601
    await cache.check("ET26-SUSP-KKKK")  # no raise
    assert "ET26-SUSP-KKKK" not in cache._webhook_suspended


# ---- CRL parsing ------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_parses_live_crl_shape(monkeypatch):
    """Matches the live CRL: {"updated_at": ..., "revoked": [{"passport":
    ..., "revoked_at": ...}]} — with malformed entries skipped."""
    cache = _cache()

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "updated_at": "2026-07-09T00:00:00Z",
                "revoked": [
                    {"passport": "ET26-REVK-LLLL", "revoked_at": "2026-07-08T00:00:00Z"},
                    {"revoked_at": "no-passport-key"},
                    "not-a-dict",
                    {"passport": "ET26-REVK-MMMM"},
                ],
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr("app.auth.revocation.httpx.AsyncClient", _Client)
    revoked = await cache._fetch()
    assert revoked == frozenset({"ET26-REVK-LLLL", "ET26-REVK-MMMM"})


# ---- end-to-end through require_passport ------------------------------


@pytest.mark.asyncio
async def test_gated_route_rejects_revoked_passport(auth_client, ept_keypair):
    """A validly-signed, unexpired EPT whose passport is on the revocation
    list must 401 at the route — the exact P0 hole."""
    from app.main import app

    token = sign_test_ept(ept_keypair, passport="ET26-REVK-E2E1")

    resp = await auth_client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200  # valid before revocation

    app.state.revocation.blacklist("ET26-REVK-E2E1")
    resp = await auth_client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert "revoked" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_missing_revocation_state_is_503(ept_keypair):
    """No revocation cache wired → 503 service issue, not a silent allow."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from tests.auth_helpers import StubJWKSCache

    app.state.jwks_cache = StubJWKSCache(ept_keypair["jwks"])
    saved = getattr(app.state, "revocation", None)
    app.state.revocation = None
    try:
        token = sign_test_ept(ept_keypair)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 503
    finally:
        app.state.jwks_cache = None
        app.state.revocation = saved


@pytest.mark.asyncio
async def test_webhook_event_blacklists_at_the_gate(gated_client, ept_keypair):
    """passport.revoked delivery → the same EPT stops working immediately."""
    import hashlib
    import hmac as hmac_mod
    import json as json_mod

    from app.config import get_settings

    secret = "test-webhook-secret-revocation"
    token = sign_test_ept(ept_keypair, passport="ET26-REVK-WHK1")

    resp = await gated_client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200

    settings = get_settings()
    saved = settings.eternitas_webhook_secret
    settings.eternitas_webhook_secret = secret
    try:
        body = json_mod.dumps(
            {"event_type": "passport.revoked", "passport": "ET26-REVK-WHK1"}
        ).encode()
        sig = "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        resp = await gated_client.post(
            "/webhooks",
            headers={
                "X-Eternitas-Signature": sig,
                "X-Eternitas-Event": "passport.revoked",
                "Content-Type": "application/json",
            },
            content=body,
        )
        assert resp.status_code == 204
    finally:
        settings.eternitas_webhook_secret = saved

    resp = await gated_client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
