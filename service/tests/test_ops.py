"""Ops healing reads (ADR-060 gap-closing) — /ops/logs, /ops/config,
/ops/selftest. Replicates the windy-mind template's guarantees:

- EPT required on all three (Search is passport-gated end to end)
- the logs ring holds fixed-vocabulary entries only
- config is secrets-free booleans (a key-shaped-string sweep proves it)
- the selftest exercises the router's REAL path with `passed` (never
  top-level `ok`) and caches its verdict 300s — bridge quota is metered
"""
from __future__ import annotations

import json

import pytest

from app.main import app
from app.ops_log import entries, ops_log, reset_for_tests
from app.router import Router
from app.routes.ops import reset_selftest_cache_for_tests
from app.sources.stubs import StubOwnCorpusSource
from tests.auth_helpers import sign_test_ept


@pytest.fixture(autouse=True)
def _clean_ops_state():
    reset_for_tests()
    reset_selftest_cache_for_tests()
    yield
    reset_for_tests()
    reset_selftest_cache_for_tests()


@pytest.fixture
def ept(ept_keypair):
    return sign_test_ept(ept_keypair)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_ops_routes_require_ept(client):
    assert (await client.get("/ops/logs")).status_code == 401
    assert (await client.get("/ops/config")).status_code == 401
    assert (await client.post("/ops/selftest")).status_code == 401


@pytest.mark.asyncio
async def test_ops_logs_fixed_vocabulary(auth_client, ept):
    ops_log("info", "server_start")
    ops_log("error", "source_error", "brave:quota_429")
    r = await auth_client.get("/ops/logs", headers=_auth(ept))
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "windy-search"
    assert body["count"] >= 2
    for entry in body["entries"]:
        assert set(entry.keys()) <= {"ts", "level", "event", "code"}


@pytest.mark.asyncio
async def test_ops_config_is_secret_free(auth_client, ept):
    app.state.search_router = Router([StubOwnCorpusSource()])
    try:
        r = await auth_client.get("/ops/config", headers=_auth(ept))
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "windy-search"
        assert body["sources_configured"] >= 1
        for meta in body["sources"].values():
            assert set(meta.keys()) == {"configured", "fallback"}
            assert all(isinstance(v, bool) for v in meta.values())

        def _leaves(obj):
            if isinstance(obj, dict):
                for v in obj.values():
                    yield from _leaves(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from _leaves(v)
            else:
                yield obj

        for value in _leaves(body):
            assert isinstance(value, (bool, int, str))
            if isinstance(value, str):
                assert len(value) < 64, "no key-shaped opaque strings in config"
    finally:
        app.state.search_router = None


@pytest.mark.asyncio
async def test_selftest_real_router_path(auth_client, ept):
    app.state.search_router = Router([StubOwnCorpusSource()])
    try:
        r = await auth_client.post("/ops/selftest", headers=_auth(ept))
        assert r.status_code == 200
        body = r.json()
        assert body["passed"] is True
        assert "ok" not in body, "top-level ok reserved for the invoke envelope"
        assert [s["name"] for s in body["stages"]] == ["config", "search"]
        detail = body["stages"][1]["detail"]
        assert detail["results"] >= 1
        # counts + latency only — no result content in the verdict
        raw = json.dumps(body)
        assert "http" not in raw.replace("ms_total", ""), "no result URLs leak"
    finally:
        app.state.search_router = None


@pytest.mark.asyncio
async def test_selftest_no_sources_is_honest(auth_client, ept):
    app.state.search_router = Router([])
    try:
        r = await auth_client.post("/ops/selftest", headers=_auth(ept))
        body = r.json()
        assert body["passed"] is False
        assert body["stages"][0]["ok"] is False
        assert any(e["event"] == "selftest" and e["code"] == "fail" for e in entries())
    finally:
        app.state.search_router = None


@pytest.mark.asyncio
async def test_selftest_verdict_cached(auth_client, ept):
    class CountingRouter(Router):
        calls = 0

        async def route(self, request):
            CountingRouter.calls += 1
            return await super().route(request)

    app.state.search_router = CountingRouter([StubOwnCorpusSource()])
    try:
        first = (await auth_client.post("/ops/selftest", headers=_auth(ept))).json()
        second = (await auth_client.post("/ops/selftest", headers=_auth(ept))).json()
        assert first["cached"] is False
        assert second["cached"] is True
        assert CountingRouter.calls == 1, "polling must not spend bridge quota"
    finally:
        app.state.search_router = None
