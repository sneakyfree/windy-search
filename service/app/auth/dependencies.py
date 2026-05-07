"""FastAPI dependencies for EPT-gated + EII-rate-limited routes."""

from fastapi import Depends, Header, HTTPException, Request, Response

from app.auth.ept import PassportClaims, verify_ept
from app.eii import rate_limit
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
