"""Router — fan out a `SearchRequest` across configured `Source`s and
merge results into a single uniform-shape `SearchResponse`.

M1 ships a deliberately minimal router:
  * Parallel fan-out via `asyncio.gather`.
  * Per-source exceptions are caught + logged; the call never bubbles a
    bridge failure to the agent.
  * Merge = priority-sorted concatenation; per-source cap = `max_results`.
  * Final ranks are sequential across the merged list.

Future M1 sessions add:
  * M1.5  — ranker (BM25 over snippet + recency + source-priority weight)
  * M1.6  — URL canonical-hash dedup
  * M1.7  — per-source contribution cap (anti-monoculture)
  * M1.9  — privacy proxy (query rewriting before bridge calls)
  * M1.10 — aggregate telemetry

Per master plan §6 M1.2 + ADR-014.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Sequence

from app.dedup import dedup_across_sources
from app.merge import apply_per_source_cap
from app.normalization import normalize
from app.ranking import rank as bm25_rank
from app.sources.base import RawResult, Source
from app.types import (
    BridgeSource,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchStats,
)

logger = logging.getLogger(__name__)


class Router:
    """Fan out → collect → normalize → merge. Stateless; thread-safe
    given thread-safe `Source` implementations.

    Usage:

        router = Router([StubOwnCorpusSource(), StubBraveSource()])
        response = await router.route(SearchRequest(query="hello"))
    """

    def __init__(self, sources: Sequence[Source]):
        self._sources: tuple[Source, ...] = tuple(sources)

    @property
    def sources(self) -> tuple[Source, ...]:
        return self._sources

    async def route(self, request: SearchRequest) -> SearchResponse:
        """Issue the request and return a merged response.

        Best-effort: a misbehaving source never causes the whole call
        to fail. The response always carries `stats` even if every
        source failed (results list is empty in that case).
        """
        t_start = time.monotonic()
        active: list[Source] = [s for s in self._sources if s.is_configured()]

        if not active:
            logger.warning("router.route: no configured sources")
            return SearchResponse(
                id=_make_request_id(),
                results=[],
                stats=SearchStats(
                    own_corpus_results=0,
                    bridge_results=0,
                    bridges_used=[],
                    ms_total=_ms_elapsed(t_start),
                ),
            )

        cap = request.max_results
        raw_by_source: list[list[RawResult]] = list(
            await asyncio.gather(
                *(_safe_search(source, request.query, cap) for source in active),
                return_exceptions=False,  # _safe_search wraps internally
            )
        )

        # Flatten + run the merge pipeline: dedup → per-source cap →
        # final sort → slice. Each step is a separate module so M1.5
        # (BM25 ranker) can swap _sort_pairs without disturbing the
        # rest of the pipeline.
        pairs: list[tuple[Source, RawResult]] = [
            (source, raw)
            for source, raws in zip(active, raw_by_source)
            for raw in raws
        ]
        pairs = dedup_across_sources(pairs)
        pairs = apply_per_source_cap(pairs, max_results=cap)
        pairs = self._sort_pairs(pairs, request.query)

        merged = [
            normalize(raw, source, rank=i)
            for i, (source, raw) in enumerate(pairs[:cap], start=1)
        ]
        stats = self._stats(merged, t_start)

        return SearchResponse(
            id=_make_request_id(),
            results=merged,
            stats=stats,
        )

    def _sort_pairs(
        self,
        pairs: Sequence[tuple[Source, RawResult]],
        query: str,
    ) -> list[tuple[Source, RawResult]]:
        """Final ordering before slicing to `max_results`.

        M1.5: BM25(title + snippet, query) × source_priority_weight.
        Stable on ties — preserves input order (which dedup + cap
        preserved from the original fan-out order, which itself was
        source-priority order from the constructor).
        """
        return bm25_rank(pairs, query)

    def _stats(
        self,
        merged: Sequence[SearchResult],
        t_start: float,
    ) -> SearchStats:
        """Stats describe what's in the final response (post-cap, post-merge).

        `bridges_used` is the set of `bridge:*` sources that contributed
        at least one result that survived to the agent — the canonical
        signal for the master plan §4 P2 declining-bridge KPI.
        """
        own_count = 0
        bridge_count = 0
        bridges: list[BridgeSource] = []
        for r in merged:
            if r.provenance.source == BridgeSource.OWN_CORPUS:
                own_count += 1
            else:
                bridge_count += 1
                if r.provenance.source not in bridges:
                    bridges.append(r.provenance.source)
        return SearchStats(
            own_corpus_results=own_count,
            bridge_results=bridge_count,
            bridges_used=bridges,
            ms_total=_ms_elapsed(t_start),
        )


async def _safe_search(source: Source, query: str, cap: int) -> list[RawResult]:
    """Call `source.search` with crash-protection.

    Returns `[]` on any exception so the router merge can proceed with
    whatever the other sources produced.
    """
    try:
        results = await source.search(query, max_results=cap)
        return list(results[:cap])
    except Exception as e:  # noqa: BLE001 — bridges may raise anything
        logger.warning("router source=%s failed: %s", source.name, e)
        return []


def _make_request_id() -> str:
    """16 hex chars — short enough to log freely, long enough to be unique."""
    return "srch_" + secrets.token_hex(8)


def _ms_elapsed(t_start: float) -> int:
    return int((time.monotonic() - t_start) * 1000)
