"""B.5 — /web/fetch endpoint + SSRF protection tests."""

import pytest

from tests.auth_helpers import sign_test_ept
from tests.test_web_search import RecordingEternitasClient


def _public_resolver(hostname: str) -> list[str]:
    """Fixed test resolver — every name maps to a known-public IP."""
    return ["198.51.100.42"]  # TEST-NET-2; not in any blocked range


def _private_resolver(hostname: str) -> list[str]:
    return ["10.0.0.5"]


# ---- validate_fetchable_url unit tests --------------------------------


def test_validate_rejects_non_http_schemes():
    from app.web.fetch import UnsafeURLError, validate_fetchable_url

    for bad in [
        "file:///etc/passwd",
        "gopher://example.com",
        "ftp://example.com",
        "javascript:alert(1)",
    ]:
        with pytest.raises(UnsafeURLError):
            validate_fetchable_url(bad, resolver=_public_resolver)


def test_validate_rejects_localhost_aliases():
    from app.web.fetch import UnsafeURLError, validate_fetchable_url

    for bad in [
        "http://localhost",
        "http://localhost:8080/admin",
        "http://ip6-localhost/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://instance-data.ec2.internal",
    ]:
        with pytest.raises(UnsafeURLError):
            validate_fetchable_url(bad, resolver=_public_resolver)


def test_validate_rejects_internal_suffixes():
    from app.web.fetch import UnsafeURLError, validate_fetchable_url

    for bad in [
        "http://router.local",
        "http://kubelet.internal/api",
        "http://something.localdomain/",
    ]:
        with pytest.raises(UnsafeURLError):
            validate_fetchable_url(bad, resolver=_public_resolver)


def test_validate_rejects_literal_private_ips():
    from app.web.fetch import UnsafeURLError, validate_fetchable_url

    for bad in [
        "http://10.0.0.1/admin",
        "http://172.20.0.5/",
        "http://192.168.1.1/router",
        "http://127.0.0.1:6379",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata
    ]:
        with pytest.raises(UnsafeURLError) as exc:
            validate_fetchable_url(bad, resolver=_public_resolver)
        assert "blocked" in str(exc.value).lower() or "network" in str(exc.value).lower()


def test_validate_rejects_dns_resolving_to_private():
    """Even a public-looking hostname is rejected if DNS resolves to RFC1918."""
    from app.web.fetch import UnsafeURLError, validate_fetchable_url

    with pytest.raises(UnsafeURLError) as exc:
        validate_fetchable_url("http://attacker.example.com/", resolver=_private_resolver)
    assert "10.0.0.5" in str(exc.value) or "blocked" in str(exc.value).lower()


def test_validate_accepts_public_url():
    from app.web.fetch import validate_fetchable_url

    # Should not raise
    validate_fetchable_url("https://example.com/path", resolver=_public_resolver)
    validate_fetchable_url("http://example.com:8080/", resolver=_public_resolver)


# ---- /web/fetch endpoint tests ---------------------------------------


def _patch_fetch_url(monkeypatch, *, content="<html><body>Hello world</body></html>",
                    content_type="text/html",
                    final_url=None,
                    status_code=200):
    """Replace app.web.router.fetch_url with a stub."""
    from app.web.fetch import FetchResponse

    async def fake_fetch(url, *, max_chars, offset, timeout_seconds=10.0, resolver=None):
        # Mimic fetch_url's HTML-stripping behavior so the test asserts the
        # output contract, not stub internals.
        body = content
        if "html" in content_type.lower() or "<html" in body[:1000].lower():
            import re
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()
        total = len(body)
        sliced = body[offset:offset + max_chars]
        truncated = (offset + max_chars) < total
        return FetchResponse(
            final_url=final_url or url,
            status_code=status_code,
            content_type=content_type,
            content=sliced,
            total_chars=total,
            offset=offset,
            max_chars=max_chars,
            truncated=truncated,
        )

    monkeypatch.setattr("app.web.router.fetch_url", fake_fetch)


@pytest.mark.asyncio
async def test_fetch_requires_authorization(gated_client):
    resp = await gated_client.post("/web/fetch", json={"url": "https://example.com/"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_fetch_validates_url_length(gated_client, ept_keypair):
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/fetch",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "x"},  # too short
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_fetch_returns_content_and_posts_event(gated_client, ept_keypair, monkeypatch):
    from app.main import app

    recorder = RecordingEternitasClient()
    app.state.eternitas_client = recorder

    _patch_fetch_url(monkeypatch, content="<html><body>Hello world</body></html>")

    token = sign_test_ept(ept_keypair, passport="ET26-FCH-AAAA")
    resp = await gated_client.post(
        "/web/fetch",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "https://example.com/page", "max_chars": 100},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["url"] == "https://example.com/page"
    assert data["final_url"] == "https://example.com/page"
    assert data["status_code"] == 200
    assert "Hello" in data["content"]
    assert data["integrity_event_posted"] is True

    # Eternitas got the right event
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["passport"] == "ET26-FCH-AAAA"
    assert call["event_type"] == "web.fetch.completed"
    assert call["dimension"] == "reliability"
    assert call["delta_hint"] == 1
    assert call["context"]["url_host"] == "example.com"
    assert call["context"]["status_code"] == 200


@pytest.mark.asyncio
async def test_fetch_400_on_unsafe_url(gated_client, ept_keypair, monkeypatch):
    """SSRF failures from fetch_url surface as 400."""
    from app.web.fetch import UnsafeURLError

    async def boom(url, **kwargs):
        raise UnsafeURLError("test: blocked")

    monkeypatch.setattr("app.web.router.fetch_url", boom)

    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/fetch",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "http://10.0.0.1/"},
    )
    assert resp.status_code == 400
    assert "unsafe" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_fetch_502_on_upstream_error(gated_client, ept_keypair, monkeypatch):
    import httpx

    async def boom(url, **kwargs):
        raise httpx.ConnectError("timeout")

    monkeypatch.setattr("app.web.router.fetch_url", boom)

    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/fetch",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "https://example.com/"},
    )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_fetch_pagination_round_trip(gated_client, ept_keypair, monkeypatch):
    """offset + max_chars slice the body correctly."""
    from app.main import app

    app.state.eternitas_client = RecordingEternitasClient()
    big_body = "A" * 1000  # plain text — no HTML stripping
    _patch_fetch_url(monkeypatch, content=big_body, content_type="text/plain")

    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/fetch",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "https://example.com/", "offset": 100, "max_chars": 200},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["offset"] == 100
    assert data["max_chars"] == 200
    assert data["total_chars"] == 1000
    assert len(data["content"]) == 200
    assert data["truncated"] is True


@pytest.mark.asyncio
async def test_fetch_consumes_rate_limit(gated_client, ept_keypair, monkeypatch):
    from app.main import app

    app.state.eternitas_client = RecordingEternitasClient()
    app.state.score_cache.scores["ET26-FRT-AAAA"] = 100  # critical: 5/min
    _patch_fetch_url(monkeypatch, content="ok", content_type="text/plain")

    token = sign_test_ept(ept_keypair, passport="ET26-FRT-AAAA")
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(5):
        resp = await gated_client.post(
            "/web/fetch", headers=headers, json={"url": "https://x.test/"}
        )
        assert resp.status_code == 200
    blocked = await gated_client.post(
        "/web/fetch", headers=headers, json={"url": "https://x.test/"}
    )
    assert blocked.status_code == 429
