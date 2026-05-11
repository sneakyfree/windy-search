"""`POST /v1/search` — canonical agent-facing search endpoint.

Wires the `Router` (M1.2) configured at lifespan to the V1 wire contract
defined in `app/types.py` (M0.5) and `spec/openapi-v1.yaml` (M1.1).

Auth: `require_passport_with_eii_rate_limit` — EPT verify + EII tier
lookup + per-minute rate limit + `X-Eternitas-{Tier,Score}` /
`X-RateLimit-{Limit,Count}` response headers. Monthly cost cap is NOT
applied at M1 because the stub bridges cost $0; cost-cap wiring lands
in M2 when real bridges arrive (add `v1.search` to the COSTS catalog
and switch to `require_passport_with_cost_cap`).

Per master plan §6 M1.8 + ADR-014.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_passport_with_eii_rate_limit
from app.auth.ept import PassportClaims
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
    claims: PassportClaims = Depends(require_passport_with_eii_rate_limit),
) -> SearchResponse:
    """See `spec/openapi-v1.yaml` for the full contract.

    Behavior at M1:
      * Fan out to configured stub bridges (M2 brings real bridges).
      * Best-effort: a misbehaving source never causes the whole call
        to fail; the response carries valid `stats` even when every
        source returned zero results.
      * `stats.bridges_used == []` is the canonical signal for
        "answered fully from own corpus" — load-bearing KPI per
        master plan §4 P2 + §9.

    Failure mapping:
      * 401 — missing/invalid EPT (from `require_passport`).
      * 429 — per-minute rate limit exceeded (from rate-limit dep).
      * 503 — search router not configured (lifespan didn't wire it).
    """
    search_router: Router | None = getattr(request.app.state, "search_router", None)
    if search_router is None:
        raise HTTPException(
            status_code=503,
            detail="Search router not configured",
        )

    response = await search_router.route(body)
    logger.debug(
        "v1.search passport=%s id=%s n_results=%d bridges_used=%s ms=%d",
        claims.passport,
        response.id,
        len(response.results),
        [b.value for b in response.stats.bridges_used],
        response.stats.ms_total,
    )
    return response
