"""Unit tests for the stub sources (M1.3).

Stubs are deterministic — calling `search(q)` twice yields the same
result set. Stub-specific behavior covered:

  * Each stub exposes the correct (name, source_enum, priority,
    cost_per_query).
  * Results carry source_rank 1-indexed.
  * `_UnconfiguredStubSource.is_configured()` returns False; the router
    will skip it without calling `.search()`.
  * `_BrokenStubSource.search()` raises — used in router tests to assert
    fan-out isolation.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.sources.stubs import (
    StubBraveSource,
    StubGoogleSource,
    StubOwnCorpusSource,
    _BrokenStubSource,
    _UnconfiguredStubSource,
)
from app.types import BridgeSource


@pytest.mark.asyncio
async def test_brave_stub_returns_three_deterministic_results():
    source = StubBraveSource()
    r1 = await source.search("hello world")
    r2 = await source.search("hello world")

    assert len(r1) == 3
    assert [x.url for x in r1] == [x.url for x in r2]
    assert all(x.source_rank == i for i, x in enumerate(r1, start=1))
    assert source.name == "stub_brave"
    assert source.source_enum == BridgeSource.BRAVE
    assert source.priority == 10
    assert source.cost_per_query == Decimal("0.005")
    assert source.is_configured() is True


@pytest.mark.asyncio
async def test_google_stub_returns_two_with_lower_preference():
    source = StubGoogleSource()
    results = await source.search("foo")

    assert len(results) == 2
    assert source.source_enum == BridgeSource.GOOGLE
    assert source.priority == 30  # last-resort per master plan §4 P1
    assert source.cost_per_query == Decimal("0.005")


@pytest.mark.asyncio
async def test_own_corpus_stub_priority_zero_and_free():
    source = StubOwnCorpusSource()
    results = await source.search("anything")

    assert len(results) == 2
    assert source.source_enum == BridgeSource.OWN_CORPUS
    assert source.priority == 0  # always-first per master plan §4 P2
    assert source.cost_per_query == Decimal("0")


@pytest.mark.asyncio
async def test_different_queries_yield_different_urls():
    source = StubBraveSource()
    r_a = await source.search("alpha")
    r_b = await source.search("beta")
    assert {x.url for x in r_a}.isdisjoint({x.url for x in r_b})


@pytest.mark.asyncio
async def test_unconfigured_stub_reports_not_configured():
    source = _UnconfiguredStubSource()
    assert source.is_configured() is False


@pytest.mark.asyncio
async def test_unconfigured_stub_search_raises_if_called():
    """Sanity guard: if the router ever calls .search() on a source that
    reported is_configured()=False, we want to know immediately."""
    source = _UnconfiguredStubSource()
    with pytest.raises(AssertionError):
        await source.search("anything")


@pytest.mark.asyncio
async def test_broken_stub_raises_on_search():
    source = _BrokenStubSource()
    with pytest.raises(RuntimeError, match="simulated bridge failure"):
        await source.search("anything")


def test_brave_stub_results_carry_query_in_title():
    """Spot-check the rendered title format — used in the M1.8 integration
    test to assert results made it through normalization unchanged."""
    import asyncio
    results = asyncio.run(StubBraveSource().search("xyzzy"))
    assert all("xyzzy" in r.title for r in results)
