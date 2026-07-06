"""Windy Search FastAPI app — Phase B.1 scaffold.

This codon establishes the service skeleton. Subsequent codons add:
  B.2  — Eternitas passport verification middleware
  B.3  — Per-EII rate limiter
  B.4  — `web.search` endpoint
  B.5  — `web.fetch` endpoint
  B.6  — `web.browse` endpoint (Browserbase)
  B.7  — `web.extract` endpoint (Claude vision)
  B.8  — `web.research` SSE endpoint
  B.9  — Per-passport monthly cost cap
  B.10 — Cross-tenant cache
  B.11 — Deploy + DNS
  B.12 — Tool registration in Windy Fly
"""

import json
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.anthropic_client import AnthropicClient
from app.auth.dependencies import (
    require_passport,
    require_passport_with_eii_rate_limit,
)
from app.auth.ept import PassportClaims
from app.auth.jwks import JWKSCache
from app.config import get_settings
from app.eii.score_cache import IntegrityScoreCache
from app.eii.tiers import tier_for_score
from app.eternitas_client import EternitasClient
from app.router import Router
from app.routes.version import router as version_router
from app.sources.brave import BraveSource
from app.sources.google import GoogleSource
from app.sources.stubs import StubOwnCorpusSource
from app.v1.search import router as v1_router
from app.web.router import router as web_router
from app.webhooks.consumer import handle_event, verify_signature

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Redis is optional in B.1 — when None, rate limiting + cache are
    # silently disabled in later codons. Matches eternitas's posture.
    redis_client = None
    if settings.redis_url:
        try:
            redis_client = await aioredis.from_url(settings.redis_url)
            await redis_client.ping()
        except Exception:
            redis_client = None
    app.state.redis = redis_client

    # B.2 — JWKS cache for EPT verification. Lazy: the first request
    # triggers the network fetch, not startup. Tests can override this
    # by setting `app.state.jwks_cache` before the request fires.
    app.state.jwks_cache = JWKSCache(jwks_url=settings.eternitas_jwks_url)

    # B.3 — EII score cache feeds the per-tier rate limiter.
    app.state.score_cache = IntegrityScoreCache(eternitas_base_url=settings.eternitas_base_url)

    # B.4 — Eternitas event poster. Best-effort: when the platform key
    # isn't configured (B.11 deploy hasn't provisioned it yet), the
    # client lives but skips posts. Capabilities still return results;
    # only the audit trail is missing.
    app.state.eternitas_client = EternitasClient(
        base_url=settings.eternitas_base_url,
        platform_api_key=settings.eternitas_platform_api_key,
    )

    # B.7 — Anthropic client for /web/extract. Optional — when the OAuth
    # token isn't set, /web/extract returns 503 but the rest of the
    # service operates normally.
    app.state.anthropic_client = AnthropicClient(
        oauth_token=settings.anthropic_oauth_token,
        model=settings.anthropic_model,
    )

    # M2.6 — canonical `/v1/search` router. Real Brave + Google bridges;
    # sources with missing API keys stay dormant via `is_configured()=False`
    # and the router skips them. Stateless; constructed once at lifespan.
    sources = [
        BraveSource(api_key=settings.brave_search_api_key),
        GoogleSource(
            api_key=settings.google_search_api_key,
            cse_id=settings.google_cse_id,
        ),
    ]
    # The own-corpus reader is still a STUB that fabricates results (M3 brings
    # the real reader). Never serve fabricated results from production: an agent
    # acting on invented URLs is worse off than one that gets an honest empty
    # result — and the stub ranks its two fakes #1 even after real bridges are
    # keyed. Keep it only outside production, for local/dev shape testing.
    if settings.environment != "production":
        sources.insert(0, StubOwnCorpusSource())
    app.state.search_router = Router(sources)

    yield

    if redis_client is not None:
        await redis_client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Windy Search",
        description=(
            "Agent-first web search service. Every request is gated by a "
            "valid Eternitas passport (EPT JWT) and audited as integrity "
            "events upstream. Capabilities: search, fetch, browse, extract, "
            "research."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Defense-in-depth response headers. Deliberately NO Content-Security-
        Policy: /docs (Swagger UI) is a public discovery surface and a strict
        CSP would break it. setdefault so an upstream proxy's header wins.
        """
        response = await call_next(request)
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    @app.post("/webhooks", status_code=204, include_in_schema=False)
    async def webhooks_inbox(
        request: Request,
        x_eternitas_signature: str | None = Header(default=None),
        x_eternitas_event: str | None = Header(default=None),
    ) -> Response:
        """Eternitas firehose inbox.

        Verifies X-Eternitas-Signature HMAC against `eternitas_webhook_secret`,
        then routes by event_type to the consumer's handlers (currently:
        integrity.event → score cache invalidation). Always 204s so the
        eternitas dispatcher can't probe for handling success/failure.

        When `eternitas_webhook_secret` isn't configured, falls through to
        accept-and-discard — preserves the B.11-followup behavior (keep
        dispatcher's consecutive-failures counter at 0) for environments
        that haven't provisioned the secret yet.
        """
        body_bytes = await request.body()

        if settings.eternitas_webhook_secret:
            if not verify_signature(
                body_bytes,
                x_eternitas_signature,
                settings.eternitas_webhook_secret,
            ):
                # Don't reveal verification failure as 401 — that would let
                # an attacker probe the secret. Log + accept silently.
                logger.warning(
                    "webhook HMAC mismatch (event=%s, sig=%s)",
                    x_eternitas_event, (x_eternitas_signature or "")[:20],
                )
                return Response(status_code=204)

            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                logger.warning("webhook payload decode failed: %s", e)
                return Response(status_code=204)

            # Event type is also in the body; the header is for fast routing
            # but we trust the body since HMAC just covered it.
            event_type = (
                x_eternitas_event
                or payload.get("event_type")
                or payload.get("event")
                or ""
            )
            try:
                await handle_event(event_type, payload, app.state)
            except Exception:  # never let a handler bug surface as 5xx
                logger.exception("handler error for event_type=%s", event_type)

        return Response(status_code=204)

    @app.get("/health")
    async def health() -> dict:
        """Liveness probe. Always 200 unless the process is dead."""
        return {
            "status": "ok",
            "service": settings.service_name,
            "version": __version__,
            "environment": settings.environment,
        }

    @app.get("/health/ready")
    async def health_ready() -> dict:
        """Readiness probe — requires Redis if configured. B.3+ rely on
        Redis; while Redis is optional in B.1, surfacing its state lets
        deploys see degraded mode.

        Also reports how many search sources are configured. Found live
        2026-07-06: prod ran with ZERO configured bridges — every
        /v1/search honestly returned empty (post-#49) — while this probe
        said "ready". A search service with no sources is degraded and
        the probe must say so.
        """
        redis_ok = True
        if settings.redis_url:
            try:
                if app.state.redis is None:
                    redis_ok = False
                else:
                    await app.state.redis.ping()
            except Exception:
                redis_ok = False

        search_router = getattr(app.state, "search_router", None)
        sources_configured = (
            sum(1 for s in search_router.sources if s.is_configured())
            if search_router is not None
            else 0
        )

        ready = redis_ok and sources_configured > 0
        return {
            "status": "ready" if ready else "degraded",
            "redis": redis_ok,
            "sources_configured": sources_configured,
        }

    @app.get("/whoami")
    async def whoami(claims: PassportClaims = Depends(require_passport)) -> dict:
        """B.2 self-check — returns the parsed passport claims.

        Deliberately NOT rate-limited: this is a debugging endpoint with
        no external resource cost. The B.3 rate limit gates the
        capability endpoints (B.4-B.8) where actual cost lives.
        """
        return {
            "passport": claims.passport,
            "operator_id": claims.operator_id,
            "bot_name": claims.bot_name,
            "bot_type": claims.bot_type,
            "verification_tier": claims.verification_tier,
            "trust_score_legacy": claims.trust_score,
            "expires_at": claims.expires_at,
        }

    @app.get("/integrity")
    async def my_integrity(
        claims: PassportClaims = Depends(require_passport_with_eii_rate_limit),
    ) -> dict:
        """B.3 — agent self-check for current EII tier + rate-limit budget.

        First gated endpoint. Exercises the full path: EPT verify → score
        fetch (cached 5 min) → tier lookup → rate-limit check → response
        headers carrying the tier + count. Agents call this to know how
        many requests they have left before they get throttled.
        """
        score = await app.state.score_cache.get(claims.passport)
        tier = tier_for_score(score)
        return {
            "passport": claims.passport,
            "score": score,
            "tier": tier.name,
            "limit_per_minute": tier.requests_per_minute,
        }

    app.include_router(web_router)
    app.include_router(v1_router)
    app.include_router(version_router)

    return app


app = create_app()
