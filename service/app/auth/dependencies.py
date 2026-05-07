"""FastAPI dependencies for EPT-gated + EII-rate-limited routes."""

from fastapi import Depends, Header, HTTPException, Request, Response

from app.auth.ept import PassportClaims, verify_ept
from app.config import get_settings
from app.eii import cost_cap, rate_limit
from app.eii.tiers import tier_for_score


async def require_passport(
    request: Request,
    authorization: str | None = Header(default=None),
) -> PassportClaims:
    """Verify the Authorization Bearer EPT and return claims.

    Routes that need a valid passport take this as a dependency:

        @router.get("/whoami")
        async def whoami(claims: PassportClaims = Depends(require_passport)):
            return claims
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer EPT required in Authorization header")

    jwks_cache = getattr(request.app.state, "jwks_cache", None)
    if jwks_cache is None:
        # B.2 wires this in lifespan. Returning 503 (rather than 401) makes
        # mis-configured deployments observable as service issues, not
        # client auth failures.
        raise HTTPException(status_code=503, detail="EPT verification not configured")

    token = authorization[len("Bearer "):]
    return await verify_ept(token, jwks_cache)


async def require_passport_with_eii_rate_limit(
    request: Request,
    response: Response,
    claims: PassportClaims = Depends(require_passport),
) -> PassportClaims:
    """B.3 — auth + EII tier lookup + per-passport rate limit.

    The route handler receives the same `PassportClaims` it would from
    `require_passport`; the rate-limit + tier resolution happen as a
    side effect and are surfaced via response headers:

        X-Eternitas-Tier:           tier name (exceptional|trusted|...)
        X-Eternitas-Score:          current EII score (cached up to 5 min)
        X-RateLimit-Limit:          requests per minute for this tier
        X-RateLimit-Count:          requests observed in the current 60s window

    On 429, the same headers are present so the caller can compute backoff.
    """
    score_cache = getattr(request.app.state, "score_cache", None)
    if score_cache is None:
        raise HTTPException(status_code=503, detail="Score cache not configured")

    score = await score_cache.get(claims.passport)
    tier = tier_for_score(score)

    redis = getattr(request.app.state, "redis", None)
    decision = await rate_limit.check(
        redis,
        passport=claims.passport,
        limit_per_minute=tier.requests_per_minute,
        tier_name=tier.name,
    )

    response.headers["X-Eternitas-Tier"] = tier.name
    response.headers["X-Eternitas-Score"] = str(score)
    response.headers["X-RateLimit-Limit"] = str(tier.requests_per_minute)
    response.headers["X-RateLimit-Count"] = str(decision.count)

    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded ({tier.name} tier: "
                f"{tier.requests_per_minute}/min). Improve your Eternitas "
                f"Integrity Index to unlock a higher tier."
            ),
            headers={
                "Retry-After": "60",
                "X-Eternitas-Tier": tier.name,
                "X-Eternitas-Score": str(score),
                "X-RateLimit-Limit": str(tier.requests_per_minute),
                "X-RateLimit-Count": str(decision.count),
            },
        )

    return claims


def require_passport_with_cost_cap(capability: str):
    """B.9 — factory that returns a dependency gating on rate-limit AND
    monthly USD budget. Routes use it like:

        @router.post("/search", ...)
        async def web_search(
            ...,
            claims: PassportClaims = Depends(require_passport_with_cost_cap("web.search")),
        ): ...

    The cost catalog lives in app/eii/cost_cap.py:COSTS — adding a new
    capability there is enough; routes opt-in by passing the capability
    name to this factory.
    """
    async def _dep(
        request: Request,
        response: Response,
        claims: PassportClaims = Depends(require_passport_with_eii_rate_limit),
    ) -> PassportClaims:
        settings = get_settings()
        redis = getattr(request.app.state, "redis", None)
        decision = await cost_cap.charge(
            redis,
            passport=claims.passport,
            capability=capability,
            cap_usd=settings.monthly_cost_cap_usd_default,
            warning_pct=settings.monthly_cost_warning_pct,
        )

        response.headers["X-Cost-Cap-USD"] = f"{decision.cap_microcents / cost_cap.MICROCENTS_PER_USD:.2f}"
        response.headers["X-Cost-Used-USD"] = f"{decision.used_after / cost_cap.MICROCENTS_PER_USD:.6f}"
        response.headers["X-Cost-Capability"] = capability
        if decision.warning:
            response.headers["X-Cost-Warning"] = (
                f"Crossed {int(settings.monthly_cost_warning_pct * 100)}% of monthly budget"
            )

        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Monthly budget exhausted (cap "
                    f"${decision.cap_microcents / cost_cap.MICROCENTS_PER_USD:.2f}). "
                    f"Resets on the 1st."
                ),
                headers={
                    "Retry-After": "86400",
                    "X-Cost-Cap-USD": f"{decision.cap_microcents / cost_cap.MICROCENTS_PER_USD:.2f}",
                    "X-Cost-Used-USD": f"{decision.used_after / cost_cap.MICROCENTS_PER_USD:.6f}",
                    "X-Cost-Capability": capability,
                },
            )

        return claims

    return _dep
