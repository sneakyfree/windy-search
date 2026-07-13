"""Ops healing reads — ADR-060 gap-closing (replicates the windy-mind
Class-C template, windy-mind #60).

  GET  /ops/logs      — recent ops events (content-free ring; app/ops_log.py)
  GET  /ops/config    — effective runtime config, secrets redacted to booleans
  POST /ops/selftest  — one REAL canary search through the router's normal
                        path, pass/fail per stage, counts only

All three require a valid EPT (`require_passport`) — Search is EPT-gated end
to end, and the woven MCP shim forwards the remote caller's own passport.

The selftest spends REAL bridge quota (Brave/Google are metered), so its
verdict is cached for 300s — a polling agent cannot drain a bridge budget
through it. Payload uses `passed`, never top-level `ok` (the ADR-060 invoke
envelope reserves `ok`; a failing canary is still a successful observation).
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app import __version__
from app.auth.dependencies import require_passport
from app.auth.ept import PassportClaims
from app.config import get_settings
from app.ops_log import OPS_LOG_MAX, entries, ops_log
from app.types import SearchRequest

router = APIRouter(tags=["ops"])

SERVICE_NAME = "windy-search"

# Canary text is OUR constant, never caller input — content-free by definition.
SELFTEST_QUERY = "windy weather"

_SELFTEST_TTL = 300.0
_selftest_cache: dict[str, Any] | None = None
_selftest_expiry = 0.0


def reset_selftest_cache_for_tests() -> None:
    global _selftest_cache, _selftest_expiry
    _selftest_cache = None
    _selftest_expiry = 0.0


@router.get("/ops/logs")
async def ops_logs(_: PassportClaims = Depends(require_passport)) -> dict[str, Any]:
    """Recent operational events — fixed vocabulary + enum codes only
    (source failures by category, selftest verdicts, server starts).
    Never a query, a result, or a raw bridge error."""
    items = entries()
    return {
        "service": SERVICE_NAME,
        "count": len(items),
        "max": OPS_LOG_MAX,
        "entries": items,
    }


@router.get("/ops/config")
async def ops_config(
    request: Request,
    _: PassportClaims = Depends(require_passport),
) -> dict[str, Any]:
    """Effective runtime config, secrets redacted to pure booleans: which
    search sources actually hold keys (and which are fallbacks), whether
    Redis / Eternitas integrity-reporting / telemetry / the render chain
    are wired. Never key material — not even fingerprints."""
    settings = get_settings()
    search_router = getattr(request.app.state, "search_router", None)
    sources: dict[str, dict[str, bool]] = {}
    if search_router is not None:
        for source in search_router.sources:
            sources[source.name] = {
                "configured": bool(source.is_configured()),
                "fallback": bool(source.is_fallback),
            }
    return {
        "service": SERVICE_NAME,
        "version": __version__,
        "environment": settings.environment,
        "sources": sources,
        "sources_configured": sum(1 for s in sources.values() if s["configured"]),
        "redis_configured": bool(settings.redis_url),
        "integrity_reporting_configured": bool(settings.eternitas_platform_api_key),
        "telemetry_configured": bool(
            settings.windy_admin_ingest_url and settings.windy_admin_ingest_token
        ),
        "render_backends": [b.strip() for b in settings.render_backends.split(",") if b.strip()],
    }


class SelftestRequest(BaseModel):
    max_results: int = Field(default=3, ge=1, le=5, description="Canary result cap.")


@router.post("/ops/selftest")
async def ops_selftest(
    request: Request,
    body: SelftestRequest | None = None,
    _: PassportClaims = Depends(require_passport),
) -> dict[str, Any]:
    """Exercise the REAL core path: one canary query through the router
    exactly as an agent's /v1/search routes (all configured sources,
    merge, dedup). Counts and latency only — never result content."""
    global _selftest_cache, _selftest_expiry
    if _selftest_cache is not None and time.monotonic() < _selftest_expiry:
        return {**_selftest_cache, "cached": True}

    started = time.perf_counter()
    stages: list[dict[str, Any]] = []

    search_router = getattr(request.app.state, "search_router", None)
    configured = (
        [s for s in search_router.sources if s.is_configured()]
        if search_router is not None
        else []
    )
    stages.append({
        "name": "config",
        "ok": bool(configured),
        "detail": f"{len(configured)} source(s) configured",
    })

    if configured:
        try:
            response = await search_router.route(
                SearchRequest(query=SELFTEST_QUERY, max_results=(body.max_results if body else 3))
            )
            stages.append({
                "name": "search",
                "ok": len(response.results) > 0,
                "detail": {
                    "results": len(response.results),
                    "bridges_used": [str(b) for b in response.stats.bridges_used],
                    "ms_total": response.stats.ms_total,
                },
            })
        except Exception as e:  # router shields per-source errors; this is systemic
            stages.append({"name": "search", "ok": False, "detail": type(e).__name__})
    else:
        stages.append({"name": "search", "ok": False, "detail": "skipped — no sources configured"})

    passed = all(s["ok"] for s in stages)
    ops_log("info" if passed else "error", "selftest", "pass" if passed else "fail")
    verdict = {
        "passed": passed,
        "stages": stages,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
    _selftest_cache = verdict
    _selftest_expiry = time.monotonic() + _SELFTEST_TTL
    return {**verdict, "cached": False}
