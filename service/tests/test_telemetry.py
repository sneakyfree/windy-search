"""Tests for M1.10 — structured per-request telemetry on the router.

The router emits one INFO log line per `/v1/search` call with named
fields suitable for aggregation pipelines (Datadog/Grafana). These
tests assert the log's *shape*, not the rendered text — log
aggregators consume the `extra` dict fields directly.

Per master plan §6 M1.10 + ADR-014: aggregate-only; NO query body, NO
passport, NO per-user history.
"""
from __future__ import annotations

import logging

import pytest

from app.router import Router
from app.sources.stubs import (
    StubBraveSource,
    StubOwnCorpusSource,
    _BrokenStubSource,
)
from app.types import SearchRequest


def _req(query: str = "hello world", max_results: int = 10) -> SearchRequest:
    return SearchRequest(query=query, max_results=max_results)


# Required telemetry fields per master plan §6 M1.10.
REQUIRED_FIELDS = {
    "search_id",
    "n_sources_called",
    "n_sources_answered",
    "n_results_returned",
    "ms_total",
    "bridges_used",
    "privacy_redactions",
}

# Fields that MUST NOT appear in the log line.
FORBIDDEN_FIELDS = {
    "query",          # query body — privacy bound
    "passport",       # per-user attribution — privacy bound
    "passport_id",
    "user_id",
    "agent_id",
}


def _capture_router_logs(caplog) -> list[logging.LogRecord]:
    """Filter caplog records down to the router's `search.request` line."""
    return [r for r in caplog.records if r.message == "search.request"]


@pytest.mark.asyncio
async def test_telemetry_log_fires_on_successful_route(caplog):
    """Every `/v1/search` call emits exactly one `search.request` log."""
    router = Router([StubOwnCorpusSource(), StubBraveSource()])
    with caplog.at_level(logging.INFO, logger="app.router"):
        await router.route(_req())

    records = _capture_router_logs(caplog)
    assert len(records) == 1


@pytest.mark.asyncio
async def test_telemetry_log_has_required_fields(caplog):
    router = Router([StubOwnCorpusSource(), StubBraveSource()])
    with caplog.at_level(logging.INFO, logger="app.router"):
        await router.route(_req())

    record = _capture_router_logs(caplog)[0]
    for field in REQUIRED_FIELDS:
        assert hasattr(record, field), f"missing required telemetry field: {field}"


@pytest.mark.asyncio
async def test_telemetry_log_has_no_forbidden_fields(caplog):
    """Privacy bound: telemetry MUST NOT carry query body or per-user keys."""
    router = Router([StubBraveSource()])
    with caplog.at_level(logging.INFO, logger="app.router"):
        await router.route(_req(query="something with PII like grant@example.com"))

    record = _capture_router_logs(caplog)[0]
    for field in FORBIDDEN_FIELDS:
        assert not hasattr(record, field), f"forbidden telemetry field present: {field}"


@pytest.mark.asyncio
async def test_telemetry_counts_match_response(caplog):
    """Sanity check the counters against the response."""
    router = Router([StubOwnCorpusSource(), StubBraveSource()])
    with caplog.at_level(logging.INFO, logger="app.router"):
        response = await router.route(_req())

    record = _capture_router_logs(caplog)[0]
    assert record.n_sources_called == 2
    assert record.n_sources_answered == 2  # both produced results
    assert record.n_results_returned == len(response.results)
    assert record.bridges_used == [b.value for b in response.stats.bridges_used]
    assert record.search_id == response.id
    assert record.ms_total >= 0


@pytest.mark.asyncio
async def test_telemetry_records_privacy_redactions(caplog):
    router = Router([StubBraveSource()])
    with caplog.at_level(logging.INFO, logger="app.router"):
        await router.route(_req(query="contact grant@example.com about 555-123-4567"))

    record = _capture_router_logs(caplog)[0]
    # 1 email + 1 phone = 2 redactions
    assert record.privacy_redactions == 2


@pytest.mark.asyncio
async def test_telemetry_n_sources_answered_excludes_broken(caplog):
    """A source that crashed in `_safe_search` contributes 0 results;
    it's still "called" but not "answered"."""
    router = Router([
        StubBraveSource(),         # answers 3
        _BrokenStubSource(),       # crashes
    ])
    with caplog.at_level(logging.INFO, logger="app.router"):
        await router.route(_req())

    record = _capture_router_logs(caplog)[0]
    assert record.n_sources_called == 2
    assert record.n_sources_answered == 1


@pytest.mark.asyncio
async def test_telemetry_no_privacy_redactions_when_query_clean(caplog):
    router = Router([StubBraveSource()])
    with caplog.at_level(logging.INFO, logger="app.router"):
        await router.route(_req(query="best coffee in fishtown"))

    record = _capture_router_logs(caplog)[0]
    assert record.privacy_redactions == 0


@pytest.mark.asyncio
async def test_telemetry_bridges_used_excludes_collapsed_duplicates(caplog):
    """If a bridge's only contribution was a URL that got dedup'd out by
    own_corpus, it shouldn't appear in bridges_used (stats are post-merge)."""
    # Use the colliding-brave stub from test_router that produces own-corpus URLs
    # — but we can't import it here without a circular dependency. Re-create
    # a minimal scenario: two sources, both return identical URLs; own_corpus
    # wins via dedup, brave's contribution is dropped.
    from app.sources.base import RawResult
    from app.sources.stubs import _deterministic_hash

    class _ShadowBraveSource(StubBraveSource):
        @property
        def name(self):
            return "_shadow_brave"

        async def search(self, query, **opts):
            h = _deterministic_hash(("own_corpus", query))
            return [
                RawResult(
                    url=f"https://owncorpus.example/{h}/1",
                    title="shadow",
                    snippet="shadow",
                    source_rank=1,
                ),
                RawResult(
                    url=f"https://owncorpus.example/{h}/2",
                    title="shadow",
                    snippet="shadow",
                    source_rank=2,
                ),
            ]

    router = Router([StubOwnCorpusSource(), _ShadowBraveSource()])
    with caplog.at_level(logging.INFO, logger="app.router"):
        await router.route(_req())

    record = _capture_router_logs(caplog)[0]
    # Brave answered (n_sources_answered=2) but all its results got
    # dedup'd; bridges_used reflects the FINAL response, not raw fan-out.
    assert record.n_sources_called == 2
    assert record.n_sources_answered == 2
    assert record.bridges_used == []  # no surviving bridge results
