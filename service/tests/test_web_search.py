"""B.4 — /web/search endpoint tests."""

import pytest

from tests.auth_helpers import sign_test_ept


# ---- recorder helpers ------------------------------------------------


class RecordingEternitasClient:
    """Drop-in for EternitasClient that captures calls instead of HTTP."""

    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.calls: list[dict] = []

    async def submit_integrity_event(self, **kwargs) -> dict | None:
        self.calls.append(kwargs)
        if not self.configured:
            return None
        return {"event_id": 99, "delta_actual": 1}


def _patch_search_backend(monkeypatch, results, backend="brave"):
    """Patch app.web.router.search to return canned results without
    touching Brave / DDG."""
    from app.web.search import SearchResponse, SearchResult

    async def fake_search(query, limit, *, brave_api_key, timeout_seconds=8.0):
        return SearchResponse(
            results=[SearchResult(**r) for r in results],
            backend=backend,
            query=query,
        )

    monkeypatch.setattr("app.web.router.search", fake_search)


# ---- /web/search tests ----------------------------------------------


@pytest.mark.asyncio
async def test_search_requires_authorization(gated_client):
    """No EPT → 401 even before search runs."""
    resp = await gated_client.post("/web/search", json={"query": "test"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_search_validates_query_length(gated_client, ept_keypair):
    """Empty query → 422 from schema."""
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": ""},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_returns_results_and_posts_event(gated_client, ept_keypair, monkeypatch):
    """Happy path: search → results returned + integrity event posted."""
    from app.main import app

    recorder = RecordingEternitasClient()
    app.state.eternitas_client = recorder

    _patch_search_backend(monkeypatch, [
        {"url": "https://example.com/a", "title": "A", "snippet": "About A"},
        {"url": "https://example.com/b", "title": "B", "snippet": "About B"},
    ])

    token = sign_test_ept(ept_keypair, passport="ET26-SRCH-AAAA")
    resp = await gated_client.post(
        "/web/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "windy ecosystem", "limit": 5},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["query"] == "windy ecosystem"
    assert data["backend"] == "brave"
    assert len(data["results"]) == 2
    assert data["integrity_event_posted"] is True

    # Tier headers present (B.3 contract)
    assert resp.headers["X-Eternitas-Tier"] == "developing"  # default 500

    # Eternitas got the event
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["passport"] == "ET26-SRCH-AAAA"
    assert call["event_type"] == "web.search.completed"
    assert call["dimension"] == "reliability"
    assert call["delta_hint"] == 1
    assert call["source"] == "windy-search"
    assert call["context"]["backend"] == "brave"
    assert "idempotency_key" in call


@pytest.mark.asyncio
async def test_search_succeeds_when_eternitas_unconfigured(gated_client, ept_keypair, monkeypatch):
    """B.4 must not gate the search response on eternitas availability —
    the integrity event is best-effort."""
    from app.main import app

    recorder = RecordingEternitasClient(configured=False)
    app.state.eternitas_client = recorder

    _patch_search_backend(monkeypatch, [
        {"url": "https://example.com/a", "title": "A", "snippet": "A"},
    ])

    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "test"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["integrity_event_posted"] is False
    assert len(data["results"]) == 1


@pytest.mark.asyncio
async def test_search_502_when_backends_fail(gated_client, ept_keypair, monkeypatch):
    """Both backends down → 502."""
    async def boom(query, limit, *, brave_api_key, timeout_seconds=8.0):
        raise RuntimeError("backends offline")

    monkeypatch.setattr("app.web.router.search", boom)

    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "test"},
    )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_search_consumes_rate_limit_budget(gated_client, ept_keypair, monkeypatch):
    """Each successful search counts against the per-passport rate limit."""
    from app.main import app

    app.state.eternitas_client = RecordingEternitasClient()
    app.state.score_cache.scores["ET26-RATE-AAAA"] = 100  # critical: 5/min

    _patch_search_backend(monkeypatch, [{"url": "u", "title": "t", "snippet": "s"}])

    token = sign_test_ept(ept_keypair, passport="ET26-RATE-AAAA")
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(1, 6):
        resp = await gated_client.post("/web/search", headers=headers, json={"query": "q"})
        assert resp.status_code == 200, f"req {i}"

    blocked = await gated_client.post("/web/search", headers=headers, json={"query": "q"})
    assert blocked.status_code == 429
    assert blocked.headers["X-Eternitas-Tier"] == "critical"


# ---- EternitasClient unit ------------------------------------------


@pytest.mark.asyncio
async def test_eternitas_client_skips_when_not_configured():
    from app.eternitas_client import EternitasClient

    client = EternitasClient(base_url="https://api.eternitas.test", platform_api_key=None)
    assert client.configured is False
    result = await client.submit_integrity_event(
        passport="ET-X",
        event_type="x",
        dimension="reliability",
        delta_hint=1,
        source="test",
    )
    assert result is None


@pytest.mark.asyncio
async def test_eternitas_client_handles_401_gracefully():
    """Bad platform key → log + return None, don't raise."""
    import httpx

    from app.eternitas_client import EternitasClient

    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid platform API key"})

    transport = httpx.MockTransport(handler)
    import app.eternitas_client as ec_mod

    real_async_client = ec_mod.httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(transport=transport, timeout=5.0)

    ec_mod.httpx.AsyncClient = patched
    try:
        client = EternitasClient(
            base_url="https://api.eternitas.test",
            platform_api_key="et_plt_bogus",
        )
        result = await client.submit_integrity_event(
            passport="ET-X",
            event_type="x",
            dimension="reliability",
            delta_hint=1,
            source="test",
        )
        assert result is None
    finally:
        ec_mod.httpx.AsyncClient = real_async_client


@pytest.mark.asyncio
async def test_eternitas_client_returns_response_on_201():
    import httpx

    from app.eternitas_client import EternitasClient

    captured: dict = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(req.headers)
        captured["body"] = req.content.decode("utf-8")
        return httpx.Response(201, json={"event_id": 42, "delta_actual": 5})

    transport = httpx.MockTransport(handler)
    import app.eternitas_client as ec_mod

    real_async_client = ec_mod.httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(transport=transport, timeout=5.0)

    ec_mod.httpx.AsyncClient = patched
    try:
        client = EternitasClient(
            base_url="https://api.eternitas.test",
            platform_api_key="et_plt_real",
        )
        result = await client.submit_integrity_event(
            passport="ET-X",
            event_type="task.done",
            dimension="reliability",
            delta_hint=5,
            source="windy-search",
            idempotency_key="abc123",
        )
        assert result is not None
        assert result["event_id"] == 42
        assert "et_plt_real" in captured["headers"].get("x-api-key", "")
        assert "abc123" in captured["headers"].get("idempotency-key", "")
    finally:
        ec_mod.httpx.AsyncClient = real_async_client
