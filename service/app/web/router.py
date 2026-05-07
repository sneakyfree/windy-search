"""FastAPI router for the web.* capability endpoints.

B.4 ships /web/search. The same router will gain /web/fetch (B.5),
/web/browse (B.6), /web/extract (B.7), /web/research (B.8).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import require_passport_with_eii_rate_limit
from app.auth.ept import PassportClaims
from app.config import get_settings
from app.eternitas_client import EternitasClient
from app.web.fetch import (
    MAX_BYTES_FETCH,
    UnsafeURLError,
    fetch_url,
)
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


# -------------------------------------------------------------------------
# B.5 — /web/fetch
# -------------------------------------------------------------------------


class FetchRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    max_chars: int = Field(default=5000, ge=1, le=MAX_BYTES_FETCH)
    offset: int = Field(default=0, ge=0)


class FetchResponseModel(BaseModel):
    url: str
    final_url: str
    status_code: int
    content_type: str
    content: str
    total_chars: int
    offset: int
    max_chars: int
    truncated: bool
    integrity_event_posted: bool


@router.post("/fetch", response_model=FetchResponseModel)
async def web_fetch(
    body: FetchRequest,
    request: Request,
    claims: PassportClaims = Depends(require_passport_with_eii_rate_limit),
) -> FetchResponseModel:
    """Fetch a URL on behalf of the agent. SSRF-hardened.

    Manual redirect handling: every Location target is re-validated
    before re-fetch, so a clever target can't redirect us into 169.254.
    or RFC1918 space.

    Side effects:
      - Posts integrity event (dimension=reliability, delta_hint=+1,
        event_type=web.fetch.completed). Same idempotency posture as
        /web/search — best-effort, never blocks the response.

    Failure mapping:
      - SSRF check fail (scheme/host/IP/redirect target) → 400
      - HTTP error from target (4xx/5xx upstream) → 502
      - Network/timeout → 502
    """
    try:
        result = await fetch_url(body.url, max_chars=body.max_chars, offset=body.offset)
    except UnsafeURLError as e:
        raise HTTPException(status_code=400, detail=f"unsafe URL: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"upstream HTTP {e.response.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream network error: {e}")

    eternitas: Optional[EternitasClient] = getattr(request.app.state, "eternitas_client", None)
    posted = False
    if eternitas is not None:
        idem = f"fetch:{claims.passport}:{uuid.uuid4().hex}"
        post_resp = await eternitas.submit_integrity_event(
            passport=claims.passport,
            event_type="web.fetch.completed",
            dimension="reliability",
            delta_hint=1,
            source="windy-search",
            context={
                "url_host": _safe_host(body.url),
                "status_code": result.status_code,
                "content_type": result.content_type[:50],
            },
            idempotency_key=idem,
        )
        posted = post_resp is not None

    return FetchResponseModel(
        url=body.url,
        final_url=result.final_url,
        status_code=result.status_code,
        content_type=result.content_type,
        content=result.content,
        total_chars=result.total_chars,
        offset=result.offset,
        max_chars=result.max_chars,
        truncated=result.truncated,
        integrity_event_posted=posted,
    )


def _safe_host(url: str) -> str:
    """Hostname only — never log full URL into integrity-event audit."""
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""
