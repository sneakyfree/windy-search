"""Unit tests for app/normalization.py (M1.4).

Covers:
  * RawResult → SearchResult shape conformance (url/title/snippet/rank).
  * Provenance shape varies by source type:
      - bridge results carry `fetched_at`, no `indexed_at`/`domain_ei`.
      - own-corpus results carry neither at M1 (M3 reader populates).
  * `rank` is whatever the router supplies — normalization is dumb about
    final ordering.
  * `fetched_at` defaults to "now" (ISO-8601 with trailing Z).
"""
from __future__ import annotations

import re

from app.normalization import normalize
from app.sources.base import RawResult
from app.sources.stubs import (
    StubBraveSource,
    StubOwnCorpusSource,
)
from app.types import BridgeSource


def _raw(url: str = "https://example.com/a") -> RawResult:
    return RawResult(
        url=url,
        title="A title",
        snippet="A snippet.",
        source_rank=1,
    )


def test_bridge_result_shape():
    source = StubBraveSource()
    normalized = normalize(_raw(), source, rank=1)

    assert normalized.url == "https://example.com/a"
    assert normalized.title == "A title"
    assert normalized.snippet == "A snippet."
    assert normalized.rank == 1
    assert normalized.provenance.source == BridgeSource.BRAVE
    assert normalized.provenance.fetched_at is not None
    assert normalized.provenance.indexed_at is None
    assert normalized.provenance.domain_ei is None
    assert normalized.provenance.agent_friendliness_score is None


def test_bridge_fetched_at_is_iso8601_z():
    """Default fetched_at should be ISO-8601 UTC with trailing Z."""
    source = StubBraveSource()
    normalized = normalize(_raw(), source, rank=1)
    fa = normalized.provenance.fetched_at
    assert fa is not None
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", fa), fa


def test_bridge_explicit_fetched_at():
    source = StubBraveSource()
    normalized = normalize(
        _raw(),
        source,
        rank=1,
        fetched_at="2026-05-11T12:34:56Z",
    )
    assert normalized.provenance.fetched_at == "2026-05-11T12:34:56Z"


def test_own_corpus_result_shape():
    source = StubOwnCorpusSource()
    normalized = normalize(_raw(), source, rank=5)

    assert normalized.rank == 5
    assert normalized.provenance.source == BridgeSource.OWN_CORPUS
    # M1 stubs: both timestamps are None. M3 reader will populate
    # indexed_at when it ships.
    assert normalized.provenance.indexed_at is None
    assert normalized.provenance.fetched_at is None


def test_rank_is_passed_through_unmodified():
    """Normalization doesn't re-rank — it just stamps what the router
    decided."""
    source = StubBraveSource()
    for r in (1, 7, 50):
        out = normalize(_raw(), source, rank=r)
        assert out.rank == r


def test_serializes_with_underscore_provenance_alias():
    """The wire shape uses `_provenance` as the field name."""
    source = StubBraveSource()
    normalized = normalize(_raw(), source, rank=1)
    dumped = normalized.model_dump(by_alias=True)
    assert "_provenance" in dumped
    assert "provenance" not in dumped
    assert dumped["_provenance"]["source"] == "bridge:brave"
