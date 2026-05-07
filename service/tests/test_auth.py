"""B.2 — Eternitas passport verification middleware tests."""

import pytest

from tests.auth_helpers import (
    StubJWKSCache,
    generate_ept_keypair,
    sign_test_ept,
)


@pytest.mark.asyncio
async def test_whoami_requires_authorization_header(client):
    """No Authorization header → 401."""
    resp = await client.get("/whoami")
    assert resp.status_code == 401
    assert "EPT" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_whoami_rejects_non_bearer(client):
    resp = await client.get("/whoami", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_whoami_503_when_jwks_unconfigured(client):
    """If app.state.jwks_cache is missing, return 503 not 401 — surfaces
    misconfiguration as a service issue rather than blaming the client."""
    from app.main import app

    saved = getattr(app.state, "jwks_cache", None)
    app.state.jwks_cache = None
    try:
        resp = await client.get("/whoami", headers={"Authorization": "Bearer dummy"})
        assert resp.status_code == 503
    finally:
        app.state.jwks_cache = saved


@pytest.mark.asyncio
async def test_whoami_accepts_valid_ept(auth_client, ept_keypair):
    token = sign_test_ept(ept_keypair, passport="ET26-VALI-DAAA")
    resp = await auth_client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["passport"] == "ET26-VALI-DAAA"
    assert data["bot_type"] == "agent"
    assert data["verification_tier"] == "verified"


@pytest.mark.asyncio
async def test_whoami_rejects_malformed_jwt(auth_client):
    resp = await auth_client.get(
        "/whoami", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_whoami_rejects_unknown_kid(auth_client, ept_keypair):
    """Token signed by a key whose kid isn't in our JWKS → 401."""
    other = generate_ept_keypair(kid="other-key")
    token = sign_test_ept(other)
    resp = await auth_client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "Unknown" in resp.json()["detail"] or "kid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_whoami_rejects_expired_ept(auth_client, ept_keypair):
    token = sign_test_ept(ept_keypair, expires_in=-60)
    resp = await auth_client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_whoami_rejects_wrong_issuer(auth_client, ept_keypair):
    token = sign_test_ept(ept_keypair, issuer="impostor.example.com")
    resp = await auth_client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_whoami_rejects_revoked_flag(auth_client, ept_keypair):
    token = sign_test_ept(ept_keypair, revoked=True)
    resp = await auth_client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "revoked" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_whoami_rejects_signature_from_wrong_key(auth_client, ept_keypair):
    """Token signed with private_other but kid claims our public — JWT
    verify catches the mismatched signature."""
    other = generate_ept_keypair(kid=ept_keypair["kid"])  # same kid, different key
    token = sign_test_ept(other)
    resp = await auth_client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_jwks_cache_unit():
    """Unit-test the StubJWKSCache contract — find_key resolves matching
    kids, returns None for unknown ones."""
    kp = generate_ept_keypair("k-x")
    cache = StubJWKSCache(kp["jwks"])
    found = await cache.find_key("k-x")
    assert found is not None
    assert found["kid"] == "k-x"

    missing = await cache.find_key("k-doesnotexist")
    assert missing is None
