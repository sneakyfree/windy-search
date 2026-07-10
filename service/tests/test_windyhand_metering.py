"""Single-meter rule under a failover chain.

`_charge_browse_topup` now charges web.browse PESSIMISTICALLY (the budget
gate — we don't know which backend serves until after the render).
`_settle_single_meter` then refunds it iff our own Windy Hand fleet served
(the fleet self-meters web.render on the forwarded EPT). A fall-through to
Browserbase keeps the charge."""

from __future__ import annotations

from types import SimpleNamespace

from app.eii import cost_cap
from app.web.router import _charge_browse_topup, _settle_single_meter


def _req():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis=None)),
        state=SimpleNamespace(cost_cap_usd=5.0, cost_warning_pct=0.8),
    )


async def test_topup_always_charges_browse_pessimistically():
    claims = SimpleNamespace(passport="ET26-TEST-METER")
    decision = await _charge_browse_topup(_req(), claims)
    assert decision is not None
    assert decision.capability == "web.browse"


async def test_settle_refunds_when_windy_hand_served():
    claims = SimpleNamespace(passport="ET26-TEST-METER")
    browse = await _charge_browse_topup(_req(), claims)
    # Our own fleet served → refund the pessimistic web.browse (fleet metered
    # web.render). Returns None so budget fields don't double-count.
    settled = await _settle_single_meter(_req(), claims, browse, "windy-hand")
    assert settled is None


async def test_settle_keeps_charge_when_browserbase_served():
    claims = SimpleNamespace(passport="ET26-TEST-METER")
    browse = await _charge_browse_topup(_req(), claims)
    settled = await _settle_single_meter(_req(), claims, browse, "browserbase")
    assert settled is browse  # fall-through to Browserbase → charge stands
    assert settled.capability == "web.browse"


def test_web_browse_cost_unchanged():
    assert cost_cap.COSTS["web.browse"] == 50_000  # $0.05 — the gate amount
