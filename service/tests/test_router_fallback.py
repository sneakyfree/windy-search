"""G7 — fallback tier + bridge-degraded alerting (router).

Two failure modes this closes:
  * Silent empty: primaries exhausted/broken → the router now escalates to
    fallback sources (Google CSE) instead of returning an empty 200.
  * Silent degradation: auth/billing-class bridge failures (401/402/403/429)
    now emit a throttled `search.bridge_degraded` admin alert.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

import app.router as router_module
from app.router import Router, _alert_bridge_degraded
from app.sources.base import RawResult, Source
from app.sources.stubs import StubBraveSource, StubGoogleSource
from app.types import BridgeSource, SearchRequest


def _req(query: str = "hello world", max_results: int = 10) -> SearchRequest:
    return SearchRequest(query=query, max_results=max_results)


class _FallbackStub(StubGoogleSource):
    """Stub google results, marked as the fallback tier."""

    def __init__(self):
        self.calls = 0

    @property
    def is_fallback(self) -> bool:
        return True

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        self.calls += 1
        return await super().search(query, **opts)


class _EmptyPrimary(Source):
    """A configured primary that answers with zero results."""

    def __init__(self):
        self.calls = 0

    @property
    def name(self) -> str:
        return "empty_primary"

    @property
    def source_enum(self) -> BridgeSource:
        return BridgeSource.BRAVE

    @property
    def priority(self) -> int:
        return 10

    @property
    def cost_per_query(self) -> Decimal:
        return Decimal("0.005")

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        self.calls += 1
        return []


class _HttpErrorPrimary(_EmptyPrimary):
    """A primary that fails with a given HTTP status (Brave 429/401 class)."""

    def __init__(self, status_code: int):
        super().__init__()
        self._status_code = status_code

    @property
    def name(self) -> str:
        return "http_error_primary"

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        self.calls += 1
        request = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")
        response = httpx.Response(self._status_code, request=request)
        raise httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.fixture
def emitted(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def _capture(event_type, **kwargs):
        calls.append((event_type, kwargs))

    monkeypatch.setattr(router_module.telemetry, "emit", _capture)
    # each test starts with a clean alert throttle
    monkeypatch.setattr(router_module, "_last_alert_at", {})
    return calls


# ---------------------------------------------------------------------------
# Fallback engagement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_not_called_when_primary_answers(emitted):
    fallback = _FallbackStub()
    router = Router([StubBraveSource(), fallback])

    response = await router.route(_req())

    assert fallback.calls == 0
    assert len(response.results) == 3  # brave stub's contribution only
    assert BridgeSource.GOOGLE not in response.stats.bridges_used
    assert emitted == []


@pytest.mark.asyncio
async def test_fallback_engages_when_primaries_empty(emitted):
    fallback = _FallbackStub()
    primary = _EmptyPrimary()
    router = Router([primary, fallback])

    response = await router.route(_req())

    assert primary.calls == 1
    assert fallback.calls == 1
    assert len(response.results) == 2  # google stub's contribution
    assert response.stats.bridges_used == [BridgeSource.GOOGLE]
    assert [e for e, _ in emitted] == ["search.fallback_used"]
    assert emitted[0][1]["metadata"]["fallback_sources"] == ["stub_google"]


@pytest.mark.asyncio
async def test_fallback_engages_when_primary_raises(emitted):
    fallback = _FallbackStub()
    router = Router([_HttpErrorPrimary(429), fallback])

    response = await router.route(_req())

    assert fallback.calls == 1
    assert len(response.results) == 2
    events = [e for e, _ in emitted]
    assert "search.bridge_degraded" in events
    assert "search.fallback_used" in events


@pytest.mark.asyncio
async def test_fallback_only_config_still_answers(emitted):
    """Only Google keyed (Brave dead/unkeyed) → fallback is promoted to
    primary rather than never being queried."""
    fallback = _FallbackStub()
    router = Router([fallback])

    response = await router.route(_req())

    assert fallback.calls == 1
    assert len(response.results) == 2
    # promoted to primary — no "fallback engaged" alarm for the steady state
    assert [e for e, _ in emitted] == []


@pytest.mark.asyncio
async def test_no_fallback_still_returns_empty_200_shape(emitted):
    router = Router([_EmptyPrimary()])
    response = await router.route(_req())
    assert response.results == []
    assert response.stats.ms_total >= 0


# ---------------------------------------------------------------------------
# Degrade alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 402, 403, 429])
async def test_degraded_statuses_alert(emitted, status_code):
    router = Router([_HttpErrorPrimary(status_code)])

    await router.route(_req())

    alerts = [(e, k) for e, k in emitted if e == "search.bridge_degraded"]
    assert len(alerts) == 1
    _, kwargs = alerts[0]
    assert kwargs["provider"] == "http_error_primary"
    assert kwargs["actor_type"] == "system"
    assert kwargs["metadata"]["status_code"] == status_code


@pytest.mark.asyncio
async def test_transient_5xx_does_not_alert(emitted):
    router = Router([_HttpErrorPrimary(503)])

    response = await router.route(_req())

    assert response.results == []
    assert [e for e, _ in emitted if e == "search.bridge_degraded"] == []


@pytest.mark.asyncio
async def test_alert_is_throttled_per_source(emitted):
    """An exhausted bridge under agent traffic must not 429-storm the
    admin ledger — one alert per source per interval."""
    router = Router([_HttpErrorPrimary(429)])

    for _ in range(5):
        await router.route(_req())

    alerts = [e for e, _ in emitted if e == "search.bridge_degraded"]
    assert len(alerts) == 1


def test_alert_fires_again_after_interval(emitted, monkeypatch):
    source = _HttpErrorPrimary(429)
    _alert_bridge_degraded(source, 429)
    # simulate the interval elapsing
    router_module._last_alert_at[source.name] -= router_module._ALERT_INTERVAL_S + 1
    _alert_bridge_degraded(source, 429)

    alerts = [e for e, _ in emitted if e == "search.bridge_degraded"]
    assert len(alerts) == 2
