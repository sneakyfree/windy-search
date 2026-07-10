"""Single-meter rule: when the render backend is Windy Hand, /web/fetch
must NOT charge web.browse (the fleet meters web.render on the forwarded
EPT). Browserbase (rented) still gets the web.browse top-up."""

from __future__ import annotations

from types import SimpleNamespace

from app.web.router import _charge_browse_topup


def _req(via: str | None):
    """Minimal fake Request exposing what _charge_browse_topup reads."""
    backend = SimpleNamespace(via=via)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(render_backend=backend, redis=None)),
        state=SimpleNamespace(cost_cap_usd=5.0, cost_warning_pct=0.8),
    )


async def test_windyhand_backend_skips_browse_charge():
    claims = SimpleNamespace(passport="ET26-TEST-METER")
    decision = await _charge_browse_topup(_req("windy-hand"), claims)
    assert decision is None  # single meter → no search-side web.browse charge


async def test_browserbase_backend_still_charges_browse():
    claims = SimpleNamespace(passport="ET26-TEST-METER")
    decision = await _charge_browse_topup(_req("browserbase"), claims)
    assert decision is not None
    assert decision.capability == "web.browse"


async def test_unknown_backend_still_charges_browse():
    claims = SimpleNamespace(passport="ET26-TEST-METER")
    decision = await _charge_browse_topup(_req(None), claims)
    assert decision is not None
    assert decision.capability == "web.browse"
