"""Windy Admin telemetry emission (ADR-WA-001) — app/telemetry.py.

(Distinct from test_telemetry.py, which covers M1.10 structured router
logs.) The contract under test: emit() is inert when unconfigured,
posts a well-formed envelope when configured, and cost_cap's
charge/refund paths each produce their ledger event — without ever
affecting the CostDecision itself.
"""

import asyncio

import pytest

from app import telemetry
from app.config import get_settings


@pytest.fixture
def configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "windy_admin_ingest_url", "http://admin-api:8900")
    monkeypatch.setattr(settings, "windy_admin_ingest_token", "wat_test")
    sent: list[list[dict]] = []

    async def fake_send(events):
        sent.append(events)

    monkeypatch.setattr(telemetry, "_send", fake_send)
    return sent


async def _drain():
    # Let fire-and-forget tasks run.
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_emit_noop_when_unconfigured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "windy_admin_ingest_url", None)
    monkeypatch.setattr(settings, "windy_admin_ingest_token", None)
    called = []
    monkeypatch.setattr(telemetry, "_send", lambda events: called.append(events))

    telemetry.emit("cost.charge", actor_type="agent", actor_id="ET-X")
    await _drain()
    assert called == []


@pytest.mark.asyncio
async def test_emit_envelope_shape(configured):
    telemetry.emit(
        "cost.charge",
        actor_type="agent",
        actor_id="ET26-TEST-0001",
        cost_microcents=500,
        metadata={"capability": "web.search"},
    )
    await _drain()
    assert len(configured) == 1
    (event,) = configured[0]
    assert event["platform"] == "windy-search"
    assert event["service"] == "search-api"
    assert event["event_type"] == "cost.charge"
    assert event["actor_type"] == "agent"
    assert event["actor_id"] == "ET26-TEST-0001"
    assert event["cost_microcents"] == 500
    assert event["metadata"] == {"capability": "web.search"}
    assert event["ts"]


@pytest.mark.asyncio
async def test_charge_emits_cost_charge(configured):
    from app.eii.cost_cap import charge
    from tests.conftest import FakeRedisB3

    decision = await charge(FakeRedisB3(), "ET-X", "web.search", cap_usd=5.0, warning_pct=0.8)
    await _drain()
    assert decision.allowed is True
    events = [e for batch in configured for e in batch]
    assert [e["event_type"] for e in events] == ["cost.charge"]
    assert events[0]["cost_microcents"] == 500
    assert events[0]["metadata"]["capability"] == "web.search"


@pytest.mark.asyncio
async def test_denied_charge_emits_cost_denied(configured):
    from app.eii.cost_cap import charge
    from tests.conftest import FakeRedisB3

    redis = FakeRedisB3()
    # $0.0005 cap = 500 microcents — one web.search fills it exactly,
    # so the second charge is denied.
    await charge(redis, "ET-X", "web.search", cap_usd=0.0005, warning_pct=0.8)
    decision = await charge(redis, "ET-X", "web.search", cap_usd=0.0005, warning_pct=0.8)
    await _drain()
    assert decision.allowed is False
    events = [e for batch in configured for e in batch]
    assert [e["event_type"] for e in events] == ["cost.charge", "cost.denied"]
    assert events[1]["cost_microcents"] == 0


@pytest.mark.asyncio
async def test_refund_emits_cost_refund(configured):
    from app.eii.cost_cap import charge, refund
    from tests.conftest import FakeRedisB3

    redis = FakeRedisB3()
    await charge(redis, "ET-X", "web.search", cap_usd=5.0, warning_pct=0.8)
    await refund(redis, "ET-X", "web.search")
    await _drain()
    events = [e for batch in configured for e in batch]
    assert [e["event_type"] for e in events] == ["cost.charge", "cost.refund"]
    assert events[1]["cost_microcents"] == 500


@pytest.mark.asyncio
async def test_telemetry_failure_never_breaks_charge(configured, monkeypatch):
    from app.eii.cost_cap import charge
    from tests.conftest import FakeRedisB3

    async def exploding_send(events):
        raise RuntimeError("ingest down")

    monkeypatch.setattr(telemetry, "_send", exploding_send)
    decision = await charge(FakeRedisB3(), "ET-X", "web.search", cap_usd=5.0, warning_pct=0.8)
    await _drain()
    assert decision.allowed is True
    assert decision.cost_charged == 500
