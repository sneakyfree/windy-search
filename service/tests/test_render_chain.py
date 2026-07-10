"""Ordered failover render chain — native-first, auto-fall-through."""

from __future__ import annotations

import pytest

from app.web.fetch import FetchResponse
from app.web.render_chain import (
    AllBackendsFailedError,
    build_chain,
    chain_has_configured,
    render_with_failover,
)


class _Backend:
    def __init__(self, via, *, configured=True, fail=False):
        self.via = via
        self._configured = configured
        self._fail = fail
        self.calls = 0

    def is_configured(self):
        return self._configured

    async def render(self, url, *, ept=None):
        self.calls += 1
        if self._fail:
            raise RuntimeError(f"{self.via} boom")
        t = f"{self.via} rendered {url}"
        return FetchResponse(url, 200, "text/plain", t, len(t), 0, len(t), False)


def test_build_chain_preserves_order_and_drops_unknown():
    wh, bb = _Backend("windy-hand"), _Backend("browserbase")
    by_name = {"windy-hand": wh, "browserbase": bb}
    chain = build_chain(["windy-hand", "nope", "browserbase"], by_name)
    assert chain == [wh, bb]


def test_default_browserbase_only_chain():
    bb = _Backend("browserbase")
    chain = build_chain(["browserbase"], {"windy-hand": _Backend("windy-hand"), "browserbase": bb})
    assert chain == [bb]
    assert chain_has_configured(chain) is True


def test_chain_has_configured_false_when_none_configured():
    chain = build_chain(["windy-hand"], {"windy-hand": _Backend("windy-hand", configured=False)})
    assert chain_has_configured(chain) is False


async def test_first_backend_wins_no_fallback():
    wh, bb = _Backend("windy-hand"), _Backend("browserbase")
    resp, via, fell = await render_with_failover([wh, bb], "https://x/", ept="t")
    assert via == "windy-hand"
    assert fell == []            # no fallback used
    assert wh.calls == 1 and bb.calls == 0
    assert "windy-hand rendered" in resp.content


async def test_falls_through_to_browserbase_on_native_failure():
    wh, bb = _Backend("windy-hand", fail=True), _Backend("browserbase")
    resp, via, fell = await render_with_failover([wh, bb], "https://x/", ept="t")
    assert via == "browserbase"          # user still gets a render
    assert fell == ["windy-hand"]        # native failed, fell through
    assert wh.calls == 1 and bb.calls == 1
    assert "browserbase rendered" in resp.content


async def test_skips_unconfigured_backend():
    wh = _Backend("windy-hand", configured=False)
    bb = _Backend("browserbase")
    resp, via, fell = await render_with_failover([wh, bb], "https://x/", ept="t")
    assert via == "browserbase"
    assert wh.calls == 0                  # unconfigured never called
    assert fell == []                     # skipping isn't a "fallback"


async def test_all_backends_fail_raises():
    wh, bb = _Backend("windy-hand", fail=True), _Backend("browserbase", fail=True)
    with pytest.raises(AllBackendsFailedError):
        await render_with_failover([wh, bb], "https://x/", ept="t")


async def test_empty_chain_raises():
    with pytest.raises(AllBackendsFailedError):
        await render_with_failover([], "https://x/", ept="t")
