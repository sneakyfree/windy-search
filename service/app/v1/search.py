"""`POST /v1/search` — canonical agent-facing search endpoint.

Wires the `Router` (M1.2) configured at lifespan to the V1 wire contract
defined in `app/types.py` (M0.5) and `spec/openapi-v1.yaml` (M1.1).

Auth (M2): `require_passport_with_cost_cap("v1.search")` — full chain
of EPT verify + EII tier lookup + per-minute rate limit + per-month
USD cost cap. Emits `X-Eternitas-{Tier,Score}`, `X-RateLimit-{Limit,
Count}`, and `X-Cost-{Cap-USD,Used-USD,Capability,Tier,
Tier-Multiplier}` response headers per OpenAPI spec.

Cost-cap semantics (M2.5):
  * The dep pre-charges `COSTS["v1.search"]` = $0.01 (pessimistic;
    covers Brave + Google fan-out).
  * The handler refunds the FULL charge when `stats.bridges_used` is
    empty — that's the canonical signal for "answered fully from own
    corpus" per master plan §4 P2 + §9. Mirrors the cache-hit refund
    pattern on `/web/search`.
  * Partial refunds (e.g., only Brave ran, not Google) are not
    implemented at M2 — overcharge is at most $0.005/call, which
    rounds out across a billing cycle. Add partial-refund logic in
    a follow-on PR if telemetry shows the overcharge is meaningful.

Per master plan §6 M1.8 + M2.4-2.5 + ADR-014.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_passport_with_cost_cap
from app.auth.ept import PassportClaims
from app.eii import cost_cap
from app.router import Router
from app.types import SearchRequest, SearchResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["v1"])


@router.post(
    "/search",
    response_model=SearchResponse,
    response_model_by_alias=True,  # emit `_provenance` not `provenance`
    summary="Search the open web (agent-facing canonical V1 endpoint)",
)
async def v1_search(
    body: SearchRequest,
    request: Request,
    claims: PassportClaims = Depends(require_passport_with_cost_cap("v1.search")),
) -> SearchResponse:
    """See `spec/openapi-v1.yaml` for the full contract.

    Behavior at M2:
      * Fan out to configured `Source`s (M2 brings real Brave + Google
        adapters alongside the still-stubbed own-corpus reader).
      * Best-effort: a misbehaving source never causes the whole call
        to fail; the response carries valid `stats` even when every
        source returned zero results.
      * `stats.bridges_used == []` ⇒ answered fully from own corpus —
        canonical KPI per master plan §4 P2 + §9. Triggers cost-cap
        refund of the pessimistic pre-charge.
      * Privacy: original query stays in-process for the ranker; only
        the sanitized version reaches external bridges (M1.9).
      * Telemetry: one structured INFO `search.request` log per call
        (M1.10).

    Failure mapping:
      * 401 — missing/invalid EPT.
      * 429 — per-minute rate limit OR per-month USD budget exhausted.
      * 503 — search router not configured.
    """
    search_router: Router | None = getattr(request.app.state, "search_router", None)
    if search_router is None:
        raise HTTPException(
            status_code=503,
            detail="Search router not configured",
        )

    response = await search_router.route(body)

    # M2.5 cost-cap refund: when no external bridge ran (own-corpus-only
    # answer), refund the pessimistic $0.01 charge. Best-effort — a
    # refund failure does not surface to the agent.
    if not response.stats.bridges_used:
        redis = getattr(request.app.state, "redis", None)
        try:
            await cost_cap.refund(redis, claims.passport, "v1.search")
        except Exception:  # noqa: BLE001 — never fail the response on a refund hiccup
            logger.exception(
                "v1.search refund failed (passport=%s, id=%s)",
                claims.passport,
                response.id,
            )

    return response
