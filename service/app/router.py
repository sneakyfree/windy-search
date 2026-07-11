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

import httpx

from app import telemetry
from app.dedup import dedup_across_sources
from app.merge import apply_per_source_cap
from app.normalization import normalize
from app.privacy import rewrite_query
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
        configured: list[Source] = [s for s in self._sources if s.is_configured()]
        primaries = [s for s in configured if not s.is_fallback]
        fallbacks = [s for s in configured if s.is_fallback]
        # Fallbacks only ever run when the primaries came up empty, so a
        # fallback-only configuration should still answer queries.
        if not primaries:
            primaries, fallbacks = fallbacks, []

        if not primaries:
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

        # M1.9 — privacy proxy: strip PII before external bridges see the
        # query. The ORIGINAL query stays in-process for the ranker so
        # BM25 still scores against the user's intent.
        sanitized_query, n_redactions = rewrite_query(request.query)

        active = primaries
        raw_by_source: list[list[RawResult]] = list(
            await asyncio.gather(
                *(_safe_search(source, sanitized_query, cap) for source in active),
                return_exceptions=False,  # _safe_search wraps internally
            )
        )

        # G7 — fallback tier: every primary came back empty (exhausted
        # Brave credits, auth failure, outage). Rather than returning a
        # silent empty 200, escalate to the last-resort bridges.
        if fallbacks and not any(raw_by_source):
            logger.warning(
                "router.route: all %d primary sources empty — engaging %d fallback source(s): %s",
                len(active),
                len(fallbacks),
                [s.name for s in fallbacks],
            )
            telemetry.emit(
                "search.fallback_used",
                actor_type="system",
                metadata={
                    "primary_count": len(active),
                    "fallback_sources": [s.name for s in fallbacks],
                },
            )
            active = active + fallbacks
            raw_by_source += list(
                await asyncio.gather(
                    *(_safe_search(source, sanitized_query, cap) for source in fallbacks),
                    return_exceptions=False,
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
        request_id = _make_request_id()

        # M1.10 — aggregate-only structured telemetry. NO query body, NO
        # passport — per master plan §4 P6 + §6 M1.10. Log aggregators
        # (Datadog/Grafana) roll these up over time without needing
        # per-user keys.
        logger.info(
            "search.request",
            extra={
                "search_id": request_id,
                "n_sources_called": len(active),
                "n_sources_answered": sum(1 for raws in raw_by_source if raws),
                "n_results_returned": len(merged),
                "ms_total": stats.ms_total,
                "bridges_used": [b.value for b in stats.bridges_used],
                "privacy_redactions": n_redactions,
            },
        )

        return SearchResponse(
            id=request_id,
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


# G7 — HTTP statuses that mean a bridge is degraded in the way that
# matters commercially: bad/expired key (401/403), payment required (402),
# or credit/rate exhaustion (429). Transient 5xx/timeouts are NOT alerted —
# they self-heal and would just be noise.
_DEGRADED_STATUSES = frozenset({401, 402, 403, 429})
# One alert per source per window, so an exhausted bridge under agent
# traffic doesn't turn the admin ledger into a 429 storm (the exact
# anti-pattern CONTRACT §9 calls out on windy-mind).
_ALERT_INTERVAL_S = 300.0
_last_alert_at: dict[str, float] = {}


def _alert_bridge_degraded(source: Source, status_code: int) -> None:
    now = time.monotonic()
    last = _last_alert_at.get(source.name)
    if last is not None and (now - last) < _ALERT_INTERVAL_S:
        return
    _last_alert_at[source.name] = now
    logger.error(
        "router source=%s DEGRADED (HTTP %s) — key invalid or credits exhausted",
        source.name,
        status_code,
    )
    telemetry.emit(
        "search.bridge_degraded",
        actor_type="system",
        provider=source.name,
        metadata={
            "status_code": status_code,
            "alert_interval_s": int(_ALERT_INTERVAL_S),
        },
    )


async def _safe_search(source: Source, query: str, cap: int) -> list[RawResult]:
    """Call `source.search` with crash-protection.

    Returns `[]` on any exception so the router merge can proceed with
    whatever the other sources produced. Auth/billing-class HTTP failures
    (401/402/403/429) additionally raise a throttled `search.bridge_degraded`
    admin alert — the G7 monitor for silent bridge exhaustion.
    """
    try:
        results = await source.search(query, max_results=cap)
        return list(results[:cap])
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        if status_code in _DEGRADED_STATUSES:
            _alert_bridge_degraded(source, status_code)
        logger.warning("router source=%s failed: %s", source.name, e)
        return []
    except Exception as e:  # noqa: BLE001 — bridges may raise anything
        logger.warning("router source=%s failed: %s", source.name, e)
        return []


def _make_request_id() -> str:
    """16 hex chars — short enough to log freely, long enough to be unique."""
    return "srch_" + secrets.token_hex(8)


def _ms_elapsed(t_start: float) -> int:
    return int((time.monotonic() - t_start) * 1000)
