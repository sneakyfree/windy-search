"""FastAPI router for the web.* capability endpoints.

B.4 ships /web/search. The same router will gain /web/fetch (B.5),
/web/browse (B.6), /web/extract (B.7), /web/research (B.8).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import require_passport_with_eii_rate_limit
from app.auth.ept import PassportClaims
from app.config import get_settings
from app.eternitas_client import EternitasClient
from app.web.search import search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["web"])


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class SearchResultModel(BaseModel):
    url: str
    title: str
    snippet: str


class SearchResponseModel(BaseModel):
    query: str
    backend: str
    results: list[SearchResultModel]
    integrity_event_posted: bool


@router.post("/search", response_model=SearchResponseModel)
async def web_search(
    body: SearchRequest,
    request: Request,
    claims: PassportClaims = Depends(require_passport_with_eii_rate_limit),
) -> SearchResponseModel:
    """Run a web search via the configured backend (Brave → DDG fallback).

    Side effects:
      - Posts an `integrity.event` to eternitas: dimension=reliability,
        delta_hint=+1, event_type=`web.search.completed`. The cap at A.6
        means high-volume callers don't farm score this way — the platform
        gets at most 100 effective points/day per dimension per bot, with
        further events absorbed into audit but not score.
      - Best-effort: when eternitas is unconfigured or unreachable, the
        search result is still returned. Caller never sees the upstream
        failure.

    Future codons:
      - B.9 cost cap (per-passport monthly $ ceiling)
      - B.10 cross-tenant cache (sha256(query) keyed)
    """
    settings = get_settings()

    try:
        result = await search(
            query=body.query,
            limit=body.limit,
            brave_api_key=settings.brave_search_api_key,
        )
    except Exception as e:
        logger.exception("Search failed for passport %s: %s", claims.passport, e)
        raise HTTPException(status_code=502, detail="Search backends unreachable")

    eternitas: Optional[EternitasClient] = getattr(request.app.state, "eternitas_client", None)
    posted = False
    if eternitas is not None:
        # Idempotency-Key prevents double-credit when this handler runs
        # twice for the same (passport, query) within 24h — e.g., the
        # client retries after a network blip.
        idem = f"search:{claims.passport}:{uuid.uuid4().hex}"
        post_resp = await eternitas.submit_integrity_event(
            passport=claims.passport,
            event_type="web.search.completed",
            dimension="reliability",
            delta_hint=1,
            source="windy-search",
            context={"query_hash_prefix": body.query[:50], "backend": result.backend},
            idempotency_key=idem,
        )
        posted = post_resp is not None

    return SearchResponseModel(
        query=result.query,
        backend=result.backend,
        results=[
            SearchResultModel(url=r.url, title=r.title, snippet=r.snippet)
            for r in result.results
        ],
        integrity_event_posted=posted,
    )
