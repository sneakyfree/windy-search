"""Tests for the inbound webhook consumer (/webhooks)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest


SECRET = "6a3cb74b7d85972b23511e6c5ce3dc59baf7aef5e4920b8745a6cd99a4b4ac90"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---- verify_signature unit -------------------------------------------


def test_verify_signature_accepts_valid():
    from app.webhooks.consumer import verify_signature

    body = b'{"event_type":"integrity.event","passport":"ET26-X"}'
    assert verify_signature(body, _sign(body), SECRET) is True


def test_verify_signature_rejects_bad_secret():
    from app.webhooks.consumer import verify_signature

    body = b'{"event_type":"integrity.event"}'
    sig = _sign(body, "wrong-secret")
    assert verify_signature(body, sig, SECRET) is False


def test_verify_signature_rejects_tampered_body():
    from app.webhooks.consumer import verify_signature

    body = b'{"event_type":"integrity.event","passport":"ET26-A"}'
    sig = _sign(body)
    tampered = b'{"event_type":"integrity.event","passport":"ET26-Z"}'
    assert verify_signature(tampered, sig, SECRET) is False


def test_verify_signature_rejects_empty_header():
    from app.webhooks.consumer import verify_signature

    body = b"x"
    assert verify_signature(body, None, SECRET) is False
    assert verify_signature(body, "", SECRET) is False
    assert verify_signature(body, "no-prefix-hex", SECRET) is False


# ---- handle_event integrity.event → score cache invalidate -----------


@pytest.mark.asyncio
async def test_integrity_event_invalidates_score_cache():
    from app.webhooks.consumer import handle_event

    invalidated: list[str] = []

    class _Cache:
        def invalidate(self, passport):
            invalidated.append(passport)

    class _State:
        score_cache = _Cache()

    await handle_event(
        "integrity.event",
        {"passport": "ET26-INV-AAAA", "delta_actual": 1},
        _State(),
    )
    assert invalidated == ["ET26-INV-AAAA"]


@pytest.mark.asyncio
async def test_integrity_event_missing_passport_is_ignored():
    """Malformed payloads must not crash the handler."""
    from app.webhooks.consumer import handle_event

    class _Cache:
        def invalidate(self, passport):
            raise AssertionError("should not be called")

    class _State:
        score_cache = _Cache()

    await handle_event("integrity.event", {"no_passport_here": True}, _State())  # no raise


@pytest.mark.asyncio
async def test_unknown_event_type_is_ignored():
    from app.webhooks.consumer import handle_event

    class _State:
        score_cache = None

    await handle_event("some.unknown.event", {"x": 1}, _State())


# ---- /webhooks endpoint integration ----------------------------------


@pytest.mark.asyncio
async def test_webhooks_with_secret_invalidates_cache_on_valid_event(gated_client):
    """End-to-end: signed integrity.event → score cache entry dropped."""
    from app.config import get_settings
    from app.main import app

    # Pre-warm the score cache for a passport so we can observe invalidation
    app.state.score_cache.scores["ET26-WHK-AAAA"] = 925
    initial = await app.state.score_cache.get("ET26-WHK-AAAA")
    assert initial == 925

    # Inject the webhook secret for this test
    settings = get_settings()
    saved_secret = settings.eternitas_webhook_secret
    settings.eternitas_webhook_secret = SECRET
    try:
        body_dict = {
            "event_type": "integrity.event",
            "passport": "ET26-WHK-AAAA",
            "delta_actual": 1,
        }
        body_bytes = json.dumps(body_dict).encode()
        sig = _sign(body_bytes)

        resp = await gated_client.post(
            "/webhooks",
            headers={
                "X-Eternitas-Signature": sig,
                "X-Eternitas-Event": "integrity.event",
                "Content-Type": "application/json",
            },
            content=body_bytes,
        )
        assert resp.status_code == 204
        # The StubScoreCache.invalidate() drops the dict entry; subsequent
        # get() falls back to default_score (500).
        post_invalidation = await app.state.score_cache.get("ET26-WHK-AAAA")
        assert post_invalidation == 500
    finally:
        settings.eternitas_webhook_secret = saved_secret


@pytest.mark.asyncio
async def test_webhooks_bad_signature_does_not_invalidate(gated_client):
    """Tampered signatures must not invalidate the cache."""
    from app.config import get_settings
    from app.main import app

    app.state.score_cache.scores["ET26-WHK-BBBB"] = 925

    settings = get_settings()
    saved = settings.eternitas_webhook_secret
    settings.eternitas_webhook_secret = SECRET
    try:
        body_bytes = json.dumps({
            "event_type": "integrity.event",
            "passport": "ET26-WHK-BBBB",
        }).encode()

        resp = await gated_client.post(
            "/webhooks",
            headers={
                "X-Eternitas-Signature": "sha256=deadbeef" * 8,  # bogus
                "X-Eternitas-Event": "integrity.event",
                "Content-Type": "application/json",
            },
            content=body_bytes,
        )
        # Always 204 — don't reveal HMAC failure to attackers
        assert resp.status_code == 204
        # Cache entry intact
        assert await app.state.score_cache.get("ET26-WHK-BBBB") == 925
    finally:
        settings.eternitas_webhook_secret = saved


@pytest.mark.asyncio
async def test_webhooks_without_secret_falls_back_to_stub(gated_client):
    """When the secret isn't configured, behavior matches B.11-followup
    stub: accept-and-discard, no consumption."""
    from app.config import get_settings
    from app.main import app

    app.state.score_cache.scores["ET26-WHK-CCCC"] = 925

    settings = get_settings()
    saved = settings.eternitas_webhook_secret
    settings.eternitas_webhook_secret = None
    try:
        resp = await gated_client.post(
            "/webhooks",
            json={"event_type": "integrity.event", "passport": "ET26-WHK-CCCC"},
        )
        assert resp.status_code == 204
        # Cache untouched — no consumption when secret unset
        assert await app.state.score_cache.get("ET26-WHK-CCCC") == 925
    finally:
        settings.eternitas_webhook_secret = saved
