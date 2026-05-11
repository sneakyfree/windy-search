"""Translate a Source's `RawResult` into the canonical `SearchResult`.

The router collects `RawResult`s from each `Source`, asks normalization
to stamp provenance + ranking, and assembles the final response. Keeping
this in a separate module lets the M3 own-corpus reader and the M2 Brave
bridge plug into the same uniform shape without code duplication.

Per master plan §6 M1.4 + ADR-014.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.sources.base import RawResult, Source
from app.types import BridgeSource, Provenance, SearchResult


def normalize(
    raw: RawResult,
    source: Source,
    rank: int,
    *,
    fetched_at: str | None = None,
) -> SearchResult:
    """Convert a single `RawResult` into a canonical `SearchResult`.

    `rank` is the FINAL merged rank (1-indexed). The router assigns it
    after merging across sources.

    `fetched_at` defaults to UTC "now" when omitted. Own-corpus results
    do NOT carry `fetched_at` — they carry `indexed_at` instead (set by
    the M3 reader when it lands). For now, both timestamps are None for
    own-corpus stubs.
    """
    if source.source_enum == BridgeSource.OWN_CORPUS:
        prov = Provenance(
            source=source.source_enum,
            indexed_at=None,  # M3 reader will populate
            fetched_at=None,
            domain_ei=None,
            agent_friendliness_score=None,
        )
    else:
        prov = Provenance(
            source=source.source_enum,
            fetched_at=fetched_at or _now_iso(),
        )
    return SearchResult(
        url=raw.url,
        title=raw.title,
        snippet=raw.snippet,
        rank=rank,
        provenance=prov,
    )


def _now_iso() -> str:
    """UTC ISO-8601 with second precision and trailing Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
