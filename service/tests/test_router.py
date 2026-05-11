"""Unit tests for app/router.py (M1.2).

Covers:
  * Empty source list → empty results + zero counters but valid stats.
  * All-configured sources fan out and answer.
  * Unconfigured sources are SKIPPED (their .search() never called —
    asserted by _UnconfiguredStubSource raising if invoked).
  * Broken sources don't bubble — the call still returns the surviving
    sources' results.
  * `max_results` caps total output.
  * Priority ordering: own_corpus (priority 0) comes before bridge:brave
    (priority 10) comes before bridge:google (priority 30).
  * Per-source rank tiebreaker inside the same priority class.
  * Stats: own_corpus_results + bridge_results + bridges_used match
    what the sources produced (NOT what made it past the max_results
    cap — these are pre-cap counters per master plan §4 P2 KPI semantics).
  * Request id has `srch_` prefix.
"""
from __future__ import annotations

import pytest

from app.router import Router
from app.sources.stubs import (
    StubBraveSource,
    StubGoogleSource,
    StubOwnCorpusSource,
    _BrokenStubSource,
    _UnconfiguredStubSource,
)
from app.types import BridgeSource, SearchRequest


def _req(query: str = "hello world", max_results: int = 10) -> SearchRequest:
    return SearchRequest(query=query, max_results=max_results)


@pytest.mark.asyncio
async def test_empty_source_list_returns_empty_with_valid_stats():
    router = Router([])
    response = await router.route(_req())

    assert response.results == []
    assert response.stats.own_corpus_results == 0
    assert response.stats.bridge_results == 0
    assert response.stats.bridges_used == []
    assert response.stats.ms_total >= 0
    assert response.id.startswith("srch_")


@pytest.mark.asyncio
async def test_three_stubs_fan_out_and_merge():
    router = Router([
        StubOwnCorpusSource(),  # contributes 2
        StubBraveSource(),       # contributes 3
        StubGoogleSource(),      # contributes 2
    ])
    response = await router.route(_req())

    assert len(response.results) == 7  # 2 + 3 + 2

    # Final ranks are 1..N sequential
    assert [r.rank for r in response.results] == list(range(1, 8))


@pytest.mark.asyncio
async def test_priority_ordering_own_corpus_first_google_last():
    router = Router([
        StubGoogleSource(),       # priority 30
        StubBraveSource(),         # priority 10
        StubOwnCorpusSource(),    # priority 0
    ])
    response = await router.route(_req())
    sources_in_order = [r.provenance.source for r in response.results]

    # First two = own_corpus
    assert sources_in_order[:2] == [BridgeSource.OWN_CORPUS, BridgeSource.OWN_CORPUS]
    # Next three = brave
    assert sources_in_order[2:5] == [BridgeSource.BRAVE] * 3
    # Last two = google
    assert sources_in_order[5:7] == [BridgeSource.GOOGLE] * 2


@pytest.mark.asyncio
async def test_within_source_results_preserve_source_rank_order():
    """Brave returns 3 results with source_rank 1, 2, 3 — they must come
    out in that order after the router's tiebreaker sort."""
    router = Router([StubBraveSource()])
    response = await router.route(_req())
    snippets = [r.snippet for r in response.results]
    # Stub's snippet pattern is "Stub Brave snippet #N for ..."
    assert "#1" in snippets[0]
    assert "#2" in snippets[1]
    assert "#3" in snippets[2]


@pytest.mark.asyncio
async def test_max_results_caps_total_output():
    router = Router([
        StubOwnCorpusSource(),
        StubBraveSource(),
        StubGoogleSource(),
    ])
    # Total potential = 7 results; cap at 3
    response = await router.route(_req(max_results=3))

    assert len(response.results) == 3
    # First 3 by priority = both own_corpus + one brave
    assert response.results[0].provenance.source == BridgeSource.OWN_CORPUS
    assert response.results[1].provenance.source == BridgeSource.OWN_CORPUS
    assert response.results[2].provenance.source == BridgeSource.BRAVE


@pytest.mark.asyncio
async def test_unconfigured_source_is_skipped():
    """The unconfigured stub raises if .search() is called. Routing past
    it without raising proves the router consulted is_configured()."""
    router = Router([
        StubOwnCorpusSource(),
        _UnconfiguredStubSource(),  # would AssertionError if invoked
        StubBraveSource(),
    ])
    response = await router.route(_req())
    # 2 own_corpus + 3 brave = 5; unconfigured contributed 0
    assert len(response.results) == 5


@pytest.mark.asyncio
async def test_broken_source_does_not_bubble():
    """A bridge that raises mid-call must not fail the whole route."""
    router = Router([
        _BrokenStubSource(),
        StubBraveSource(),
    ])
    response = await router.route(_req())
    # Brave still answers; broken stub contributes 0
    assert len(response.results) == 3
    assert all(
        r.provenance.source == BridgeSource.BRAVE for r in response.results
    )


@pytest.mark.asyncio
async def test_stats_describe_final_results_not_pre_cap_fanout():
    """Stats reflect what's in the response (post-cap, post-merge), not
    the raw fan-out. With max_results=1 and own_corpus priority 0, only
    one own-corpus result survives to the response → bridges_used is
    empty even though Brave produced 3 results that were dropped."""
    router = Router([
        StubOwnCorpusSource(),  # produces 2; priority 0
        StubBraveSource(),       # produces 3; priority 10
    ])
    response = await router.route(_req(max_results=1))

    assert len(response.results) == 1
    # The one surviving result is own_corpus (priority 0 wins)
    assert response.stats.own_corpus_results == 1
    assert response.stats.bridge_results == 0
    # Brave's results didn't survive the cap; bridges_used reflects that
    assert response.stats.bridges_used == []


@pytest.mark.asyncio
async def test_stats_describe_final_results_with_room_for_bridges():
    """Same fan-out, but max_results=3 lets both own_corpus results
    plus one brave result through. bridges_used now includes brave."""
    router = Router([
        StubOwnCorpusSource(),  # produces 2; priority 0
        StubBraveSource(),       # produces 3; priority 10
    ])
    response = await router.route(_req(max_results=3))

    assert len(response.results) == 3
    assert response.stats.own_corpus_results == 2
    assert response.stats.bridge_results == 1
    assert response.stats.bridges_used == [BridgeSource.BRAVE]


@pytest.mark.asyncio
async def test_stats_empty_bridges_when_only_own_corpus():
    """Per master plan §4 P2 + §9: `bridges_used == []` is the canonical
    signal for 'answered fully from own corpus' (the declining-bridge KPI)."""
    router = Router([StubOwnCorpusSource()])
    response = await router.route(_req())

    assert response.stats.own_corpus_results == 2
    assert response.stats.bridge_results == 0
    assert response.stats.bridges_used == []


@pytest.mark.asyncio
async def test_request_id_has_srch_prefix():
    router = Router([StubBraveSource()])
    response = await router.route(_req())
    assert response.id.startswith("srch_")
    # 5 prefix chars + 16 hex chars = 21 total
    assert len(response.id) == 21


@pytest.mark.asyncio
async def test_two_calls_have_different_ids():
    """Each call gets a fresh id (no caching at the router level)."""
    router = Router([StubBraveSource()])
    r1 = await router.route(_req())
    r2 = await router.route(_req())
    assert r1.id != r2.id


@pytest.mark.asyncio
async def test_router_exposes_sources_for_introspection():
    sources = (StubOwnCorpusSource(), StubBraveSource())
    router = Router(sources)
    assert router.sources == sources


@pytest.mark.asyncio
async def test_router_handles_all_broken_sources():
    """Edge case: every source crashes. Should still return a valid
    response with empty results, not raise."""
    router = Router([_BrokenStubSource(), _BrokenStubSource()])
    response = await router.route(_req())
    assert response.results == []
    assert response.stats.own_corpus_results == 0
    assert response.stats.bridge_results == 0


# ---- M1.6 + M1.7 pipeline integration tests ----


class _CollidingBraveSource(StubBraveSource):
    """A Brave stub that returns the SAME urls as own-corpus would —
    used to test cross-source dedup in the router."""

    @property
    def name(self) -> str:
        return "_colliding_brave"

    async def search(self, query, **opts):
        from app.sources.base import RawResult
        from app.sources.stubs import _deterministic_hash
        h = _deterministic_hash(("own_corpus", query))
        # First two URLs collide with StubOwnCorpusSource; third is unique.
        return [
            RawResult(
                url=f"https://owncorpus.example/{h}/1",
                title="dup",
                snippet="dup",
                source_rank=1,
            ),
            RawResult(
                url=f"https://owncorpus.example/{h}/2",
                title="dup",
                snippet="dup",
                source_rank=2,
            ),
            RawResult(
                url="https://brave-only.example/x",
                title="unique brave",
                snippet="unique",
                source_rank=3,
            ),
        ]


@pytest.mark.asyncio
async def test_router_dedups_cross_source_duplicates():
    """Brave returns 3 results; first 2 collide with own_corpus. After
    dedup, only own_corpus's versions survive (lower priority wins);
    Brave's unique third URL also survives. Total = 2 (own) + 1 (brave) = 3."""
    router = Router([
        StubOwnCorpusSource(),
        _CollidingBraveSource(),
    ])
    response = await router.route(_req())
    assert len(response.results) == 3
    # First 2 are own_corpus (priority 0); third is brave's unique URL
    sources = [r.provenance.source for r in response.results]
    assert sources == [
        BridgeSource.OWN_CORPUS,
        BridgeSource.OWN_CORPUS,
        BridgeSource.BRAVE,
    ]
    # Stats reflect the deduped final response — brave contributed 1
    assert response.stats.own_corpus_results == 2
    assert response.stats.bridge_results == 1
    assert response.stats.bridges_used == [BridgeSource.BRAVE]


class _ManyResultsStub(StubBraveSource):
    """Returns 12 results — used to test the per-source cap binding."""

    @property
    def name(self) -> str:
        return "_many_results"

    async def search(self, query, **opts):
        from app.sources.base import RawResult
        return [
            RawResult(
                url=f"https://many.example/{i}",
                title=f"many {i}",
                snippet=f"many {i}",
                source_rank=i,
            )
            for i in range(1, 13)
        ]


@pytest.mark.asyncio
async def test_router_caps_single_source_contribution():
    """With max_results=10 + default 0.7 fraction, per-source cap = 7.
    A source that produces 12 contributes only 7 to the final list."""
    router = Router([_ManyResultsStub()])
    response = await router.route(_req(max_results=10))
    assert len(response.results) == 7  # capped, not 10 and not 12


@pytest.mark.asyncio
async def test_router_dedup_then_cap_pipeline_ordering():
    """Dedup runs BEFORE the per-source cap. If a colliding source has
    12 results and 2 collide with own-corpus, the 10 surviving brave
    results are then capped to 7."""
    router = Router([
        StubOwnCorpusSource(),  # 2 results, priority 0
        _CollidingBraveSource(),  # 3 results, 2 collide → 1 survives dedup
    ])
    response = await router.route(_req(max_results=10))
    # 2 own_corpus + 1 brave-unique = 3 total
    assert len(response.results) == 3
