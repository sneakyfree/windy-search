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

from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings


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
        version="0.1.0",
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

    @app.get("/health")
    async def health() -> dict:
        """Liveness probe. Always 200 unless the process is dead."""
        return {
            "status": "ok",
            "service": settings.service_name,
            "version": "0.1.0",
            "environment": settings.environment,
        }

    @app.get("/health/ready")
    async def health_ready() -> dict:
        """Readiness probe — requires Redis if configured. B.3+ rely on
        Redis; while Redis is optional in B.1, surfacing its state lets
        deploys see degraded mode."""
        redis_ok = True
        if settings.redis_url:
            try:
                if app.state.redis is None:
                    redis_ok = False
                else:
                    await app.state.redis.ping()
            except Exception:
                redis_ok = False
        return {
            "status": "ready" if redis_ok else "degraded",
            "redis": redis_ok,
        }

    return app


app = create_app()
