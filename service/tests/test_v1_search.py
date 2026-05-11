"""Integration tests for POST /v1/search (M1.8).

Exercises the full chain: FastAPI route → require_passport_with_eii_rate_limit
→ Router → stub bridges → normalization → SearchResponse wire shape.

Auth shortcuts:
  * `gated_client` (conftest) injects stub JWKS + ScoreCache + Redis so
    sign_test_ept-issued EPTs verify and the rate-limit dep works
    without touching network.
"""
from __future__ import annotations

import pytest

from app.main import app
from app.router import Router
from app.sources.stubs import (
    StubBraveSource,
    StubGoogleSource,
    StubOwnCorpusSource,
    _BrokenStubSource,
)
from tests.auth_helpers import sign_test_ept


@pytest.fixture(autouse=True)
def install_stub_router():
    """Default router used by every test in this module. Tests can
    override by re-assigning `app.state.search_router` before the
    request and the autouse cleanup tears it back down."""
    app.state.search_router = Router([
        StubOwnCorpusSource(),
        StubBraveSource(),
        StubGoogleSource(),
    ])
    yield
    app.state.search_router = None


@pytest.mark.asyncio
async def test_v1_search_requires_authorization(gated_client):
    """No EPT → 401 even before search runs."""
    resp = await gated_client.post("/v1/search", json={"query": "test"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_v1_search_validates_query_length(gated_client, ept_keypair):
    """Empty query → 422 from Pydantic."""
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": ""},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_v1_search_validates_max_results_upper_bound(gated_client, ept_keypair):
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "hi", "max_results": 999},  # max is 50
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_v1_search_happy_path_returns_canonical_shape(
    gated_client, ept_keypair
):
    """Three stubs configured → 7 results expected; canonical shape on
    the wire (`_provenance` not `provenance`)."""
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "hello world"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["id"].startswith("srch_")
    assert "results" in body
    assert "stats" in body

    assert len(body["results"]) == 7  # 2 own_corpus + 3 brave + 2 google

    # First result is own_corpus (priority 0)
    first = body["results"][0]
    assert "_provenance" in first
    assert "provenance" not in first  # alias serialization confirmed
    assert first["_provenance"]["source"] == "own_corpus"
    assert first["rank"] == 1


@pytest.mark.asyncio
async def test_v1_search_max_results_caps_response(gated_client, ept_keypair):
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "hello", "max_results": 3},
    )
    body = resp.json()
    assert len(body["results"]) == 3
    # First 3 by priority = both own_corpus + one brave
    sources = [r["_provenance"]["source"] for r in body["results"]]
    assert sources == ["own_corpus", "own_corpus", "bridge:brave"]


@pytest.mark.asyncio
async def test_v1_search_stats_reflect_final_response(gated_client, ept_keypair):
    """`stats.bridges_used` lists the bridges that contributed to the
    final response (post-cap). Cap=3 leaves both own_corpus + one
    brave; google's results are dropped — bridges_used = [brave]."""
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "test", "max_results": 3},
    )
    stats = resp.json()["stats"]
    assert stats["own_corpus_results"] == 2
    assert stats["bridge_results"] == 1
    assert stats["bridges_used"] == ["bridge:brave"]
    assert stats["ms_total"] >= 0


@pytest.mark.asyncio
async def test_v1_search_empty_bridges_used_when_only_own_corpus(
    gated_client, ept_keypair
):
    """Override the autouse router to expose only own-corpus → master
    plan §4 P2 canonical signal `bridges_used == []`."""
    app.state.search_router = Router([StubOwnCorpusSource()])
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "test"},
    )
    body = resp.json()
    assert len(body["results"]) == 2
    assert body["stats"]["own_corpus_results"] == 2
    assert body["stats"]["bridge_results"] == 0
    assert body["stats"]["bridges_used"] == []


@pytest.mark.asyncio
async def test_v1_search_broken_source_does_not_bubble(
    gated_client, ept_keypair
):
    """A bridge that crashes must not fail the whole call — surviving
    sources still answer."""
    app.state.search_router = Router([
        _BrokenStubSource(),
        StubBraveSource(),
    ])
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "anything"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 3  # brave's three
    assert all(
        r["_provenance"]["source"] == "bridge:brave" for r in body["results"]
    )


@pytest.mark.asyncio
async def test_v1_search_emits_rate_limit_headers(gated_client, ept_keypair):
    """The auth dep at this level emits X-Eternitas-* + X-RateLimit-*
    headers on every gated response — load-bearing per OpenAPI spec."""
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "hello"},
    )
    assert resp.status_code == 200
    assert "X-Eternitas-Tier" in resp.headers
    assert "X-Eternitas-Score" in resp.headers
    assert "X-RateLimit-Limit" in resp.headers
    assert "X-RateLimit-Count" in resp.headers


@pytest.mark.asyncio
async def test_v1_search_returns_503_when_router_not_configured(
    gated_client, ept_keypair
):
    """If lifespan failed to wire a router, the endpoint should 503
    rather than hard-fail with a confusing 500."""
    app.state.search_router = None
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "test"},
    )
    assert resp.status_code == 503
    assert "router" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_v1_search_includes_agent_context(gated_client, ept_keypair):
    """Spec lets agents pass routing-hint context; M1 router doesn't
    consume it yet but the endpoint must accept it without 422."""
    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "query": "best coffee",
            "max_results": 5,
            "agent_context": {"purpose": "find_a_place", "user_locale": "en-US"},
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_v1_search_two_calls_have_different_ids(gated_client, ept_keypair):
    """Each /v1/search call gets a fresh request id."""
    token = sign_test_ept(ept_keypair)
    r1 = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "x"},
    )
    r2 = await gated_client.post(
        "/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "x"},
    )
    assert r1.json()["id"] != r2.json()["id"]
