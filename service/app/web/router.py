"""FastAPI router for the web.* capability endpoints.

B.4 ships /web/search. The same router will gain /web/fetch (B.5),
/web/browse (B.6), /web/extract (B.7), /web/research (B.8).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import (
    require_passport_with_cost_cap,
)
from app.auth.ept import PassportClaims
from app.config import get_settings
from app.eii import cost_cap, result_cache
from app.eternitas_client import EternitasClient
from app.web.extract import extract_structured_data
from app.web.browserbase import BrowserbaseRenderer, looks_like_needs_render
from app.web.fetch import (
    MAX_BYTES_FETCH,
    UnsafeURLError,
    fetch_url,
)
from app.web.search import search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["web"])


def _idem_key(action: str, passport: str, subject: str) -> str:
    """Deterministic idempotency key so a retried request doesn't double-post an
    integrity event. Stable per (action, passport, subject) within a UTC day: a
    retry after a network blip reuses the same key (Eternitas dedups it), while a
    genuine repeat the next day gets a fresh one. Replaces a per-call uuid4 that
    made every retry look like a distinct event.
    """
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"{action}:{passport}:{digest}:{day}"


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
    cache_hit: bool = False


@router.post("/search", response_model=SearchResponseModel)
async def web_search(
    body: SearchRequest,
    request: Request,
    claims: PassportClaims = Depends(require_passport_with_cost_cap("web.search")),
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
    redis = getattr(request.app.state, "redis", None)

    # B.10 — cross-tenant query cache. Hash by (query, limit) — passport
    # is intentionally NOT in the key, so different agents share entries.
    cache_payload = {"query": body.query, "limit": body.limit}
    cached = await result_cache.get_cached(redis, "web.search", cache_payload)
    if cached is not None:
        # Refund the cost we charged in the dependency — cache hits don't
        # spend Brave credits so the cap should reflect reality.
        await cost_cap.refund(redis, claims.passport, "web.search")

        eternitas: EternitasClient | None = getattr(request.app.state, "eternitas_client", None)
        posted = False
        if eternitas is not None:
            idem = _idem_key("search", claims.passport, body.query)
            post_resp = await eternitas.submit_integrity_event(
                passport=claims.passport,
                event_type="web.search.completed",
                dimension="reliability",
                delta_hint=1,
                source="windy-search",
                context={
                    "query_hash_prefix": body.query[:50],
                    "backend": cached.get("backend", "cached"),
                    "cache_hit": True,
                },
                idempotency_key=idem,
            )
            posted = post_resp is not None

        return SearchResponseModel(
            query=cached["query"],
            backend=cached["backend"],
            results=[SearchResultModel(**r) for r in cached["results"]],
            integrity_event_posted=posted,
            cache_hit=True,
        )

    try:
        result = await search(
            query=body.query,
            limit=body.limit,
            brave_api_key=settings.brave_search_api_key,
        )
    except Exception as e:
        logger.exception("Search failed for passport %s: %s", claims.passport, e)
        raise HTTPException(status_code=502, detail="Search backends unreachable")

    # Populate cache for the next caller. Best-effort — failure here
    # doesn't surface to the user.
    cache_value = {
        "query": result.query,
        "backend": result.backend,
        "results": [
            {"url": r.url, "title": r.title, "snippet": r.snippet}
            for r in result.results
        ],
    }
    await result_cache.set_cached(redis, "web.search", cache_payload, cache_value)

    eternitas: EternitasClient | None = getattr(request.app.state, "eternitas_client", None)
    posted = False
    if eternitas is not None:
        # Idempotency-Key prevents double-credit when this handler runs
        # twice for the same (passport, query) within 24h — e.g., the
        # client retries after a network blip.
        idem = _idem_key("search", claims.passport, body.query)
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
        cache_hit=False,
    )


# -------------------------------------------------------------------------
# B.5 — /web/fetch
# -------------------------------------------------------------------------


class FetchRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    max_chars: int = Field(default=5000, ge=1, le=MAX_BYTES_FETCH)
    offset: int = Field(default=0, ge=0)
    # B.6 — render mode. "off" (default) = plain httpx, unchanged behavior.
    # "on" = always render in a Browserbase cloud browser (JS-executed).
    # "auto" = plain fetch first, escalate to a render only when the result
    # looks like an unhydrated SPA shell or a bot-wall. "on"/"auto" require
    # BROWSERBASE_API_KEY; when unset they behave like "off" (auto) or 503 (on).
    render: Literal["off", "auto", "on"] = "off"


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
    cache_hit: bool = False
    # None = plain httpx fetch; "browserbase" = rendered in a cloud browser.
    rendered_via: str | None = None


@router.post("/fetch", response_model=FetchResponseModel)
async def web_fetch(
    body: FetchRequest,
    request: Request,
    claims: PassportClaims = Depends(require_passport_with_cost_cap("web.fetch")),
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
    redis = getattr(request.app.state, "redis", None)
    renderer: BrowserbaseRenderer | None = getattr(
        request.app.state, "browserbase_renderer", None
    )

    # render="on" needs a configured Browserbase key; fail clearly if not.
    if body.render == "on" and (renderer is None or not renderer.is_configured()):
        raise HTTPException(
            status_code=503,
            detail="render='on' requires Browserbase (BROWSERBASE_API_KEY not configured)",
        )

    # B.10 — cache key includes pagination so /fetch?offset=0 and offset=100
    # don't collide. Cached value stores the FULL body (not the slice) so
    # different (offset, max_chars) combos can be served from one entry.
    # `render` is part of the key so a rendered body and a plain body for the
    # same URL never collide.
    cache_payload = {"url": body.url, "render": body.render}
    cached = await result_cache.get_cached(redis, "web.fetch", cache_payload)
    if cached is not None:
        await cost_cap.refund(redis, claims.passport, "web.fetch")

        # Re-slice the cached body for this request's pagination.
        body_full = cached["content_full"]
        sliced = body_full[body.offset:body.offset + body.max_chars]
        truncated = (body.offset + body.max_chars) < len(body_full)

        eternitas: EternitasClient | None = getattr(request.app.state, "eternitas_client", None)
        posted = False
        if eternitas is not None:
            idem = _idem_key("fetch", claims.passport, body.url)
            post_resp = await eternitas.submit_integrity_event(
                passport=claims.passport,
                event_type="web.fetch.completed",
                dimension="reliability",
                delta_hint=1,
                source="windy-search",
                context={
                    "url_host": _safe_host(body.url),
                    "status_code": cached["status_code"],
                    "content_type": cached["content_type"][:50],
                    "cache_hit": True,
                },
                idempotency_key=idem,
            )
            posted = post_resp is not None

        return FetchResponseModel(
            url=body.url,
            final_url=cached["final_url"],
            status_code=cached["status_code"],
            content_type=cached["content_type"],
            content=sliced,
            total_chars=len(body_full),
            offset=body.offset,
            max_chars=body.max_chars,
            truncated=truncated,
            integrity_event_posted=posted,
            cache_hit=True,
            rendered_via=cached.get("rendered_via"),
        )

    # rendered_via is None for the plain path, "browserbase" once we render.
    rendered_via: str | None = None

    if body.render == "on":
        # Always render — no plain fetch. (Guarded above: renderer configured.)
        try:
            result = await renderer.render(body.url)
            rendered_via = "browserbase"
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"render failed: {e}")
    else:
        # Plain fetch first (render="off" stops here; "auto" may escalate).
        try:
            # Pass max_chars = full body size so we can cache the un-sliced text.
            # The endpoint then re-slices to the caller's requested window.
            result = await fetch_url(
                body.url,
                max_chars=MAX_BYTES_FETCH,
                offset=0,
            )
        except UnsafeURLError as e:
            raise HTTPException(status_code=400, detail=f"unsafe URL: {e}")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502, detail=f"upstream HTTP {e.response.status_code}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"upstream network error: {e}")

        # render="auto": escalate to a real browser only if the plain result
        # looks like an unhydrated SPA shell or a bot-wall, and Browserbase is
        # configured. Any render failure falls back to the plain result.
        if (
            body.render == "auto"
            and renderer is not None
            and renderer.is_configured()
            and looks_like_needs_render(result)
        ):
            try:
                result = await renderer.render(body.url)
                rendered_via = "browserbase"
            except Exception as e:
                logger.info("auto-render escalation failed for %s, using plain: %s", body.url, e)

    # Cache the full decoded body so subsequent (offset, max_chars) calls
    # for the same URL share one entry.
    await result_cache.set_cached(
        redis,
        "web.fetch",
        cache_payload,
        {
            "final_url": result.final_url,
            "status_code": result.status_code,
            "content_type": result.content_type,
            "content_full": result.content,  # already the full decoded body when offset=0/max=MAX
            "rendered_via": rendered_via,
        },
    )

    # Re-slice for this request.
    body_full = result.content
    sliced = body_full[body.offset:body.offset + body.max_chars]
    truncated = (body.offset + body.max_chars) < len(body_full)
    result_for_response = type(result)(
        final_url=result.final_url,
        status_code=result.status_code,
        content_type=result.content_type,
        content=sliced,
        total_chars=len(body_full),
        offset=body.offset,
        max_chars=body.max_chars,
        truncated=truncated,
    )

    eternitas: EternitasClient | None = getattr(request.app.state, "eternitas_client", None)
    posted = False
    if eternitas is not None:
        idem = _idem_key("fetch", claims.passport, body.url)
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
                "rendered_via": rendered_via,
            },
            idempotency_key=idem,
        )
        posted = post_resp is not None

    return FetchResponseModel(
        url=body.url,
        final_url=result_for_response.final_url,
        status_code=result_for_response.status_code,
        content_type=result_for_response.content_type,
        content=result_for_response.content,
        total_chars=result_for_response.total_chars,
        offset=result_for_response.offset,
        max_chars=result_for_response.max_chars,
        truncated=result_for_response.truncated,
        integrity_event_posted=posted,
        cache_hit=False,
        rendered_via=rendered_via,
    )


def _safe_host(url: str) -> str:
    """Hostname only — never log full URL into integrity-event audit."""
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""


# -------------------------------------------------------------------------
# B.7 — /web/extract
# -------------------------------------------------------------------------


class ExtractRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    extract_schema: dict = Field(
        ..., alias="schema",
        description="JSON Schema describing the structure to extract",
    )
    instruction: str | None = Field(default=None, max_length=2000)

    # Pydantic v2 idiom — class-based Config is deprecated and removed in v3.
    model_config = {"populate_by_name": True}


class ExtractResponseModel(BaseModel):
    url: str
    final_url: str
    extracted: dict
    integrity_event_posted: bool


@router.post("/extract", response_model=ExtractResponseModel)
async def web_extract(
    body: ExtractRequest,
    request: Request,
    claims: PassportClaims = Depends(require_passport_with_cost_cap("web.extract")),
) -> ExtractResponseModel:
    """Extract JSON-Schema-shaped structured data from a URL.

    Pipeline:
      1. Fetch the URL via the existing B.5 fetch_url (SSRF-hardened,
         redirect re-validation, cache-served). HTML is stripped.
      2. Send (content, schema, optional instruction) to Claude via
         Anthropic OAuth (or Bedrock — future B.7b switch).
      3. Parse Claude's JSON output. Strip any code-fence wrapper
         the model may add despite instructions.
      4. Post integrity event.

    Failure mapping:
      - SSRF check fail (scheme/host/IP/redirect target) → 400
      - Anthropic not configured → 503
      - Anthropic returned non-200 OR non-JSON → 502
      - Upstream HTTP/network → 502
    """
    anthropic = getattr(request.app.state, "anthropic_client", None)
    if anthropic is None or not anthropic.configured:
        raise HTTPException(
            status_code=503,
            detail="Extraction unavailable — Anthropic client not configured",
        )

    try:
        fetched = await fetch_url(body.url, max_chars=MAX_BYTES_FETCH, offset=0)
    except UnsafeURLError as e:
        raise HTTPException(status_code=400, detail=f"unsafe URL: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"upstream HTTP {e.response.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream network error: {e}")

    try:
        extracted = await extract_structured_data(
            page_content=fetched.content,
            schema=body.extract_schema,
            instruction=body.instruction,
            anthropic_client=anthropic,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    eternitas: EternitasClient | None = getattr(request.app.state, "eternitas_client", None)
    posted = False
    if eternitas is not None:
        idem = _idem_key("extract", claims.passport, body.url)
        post_resp = await eternitas.submit_integrity_event(
            passport=claims.passport,
            event_type="web.extract.completed",
            dimension="reliability",
            delta_hint=1,
            source="windy-search",
            context={
                "url_host": _safe_host(body.url),
                "schema_top_level_keys": list(
                    body.extract_schema.get("properties", {}).keys()
                )[:10],
            },
            idempotency_key=idem,
        )
        posted = post_resp is not None

    return ExtractResponseModel(
        url=body.url,
        final_url=fetched.final_url,
        extracted=extracted,
        integrity_event_posted=posted,
    )
