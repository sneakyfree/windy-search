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
    """B.9 + B.9.2 — factory composing rate-limit + tier-scaled monthly
    USD budget. Routes use it like:

        @router.post("/search", ...)
        async def web_search(
            ...,
            claims: PassportClaims = Depends(require_passport_with_cost_cap("web.search")),
        ): ...

    Cap = settings.monthly_cost_cap_usd_default × tier.cost_cap_multiplier.
    Same EII score that drives the rate-limit tier (B.3) drives the
    cost-cap multiplier — one decision, two effects:

      Exceptional (900+) → 200/min, $50/month
      Trusted     (700+) → 100/min, $25/month
      Developing  (500+) →  50/min,  $5/month  ← baseline
      Watch       (400+) →  20/min,  $2/month
      Critical    (<400) →   5/min,  $0.50/month

    The cost catalog lives in app/eii/cost_cap.py:COSTS — adding a
    capability there is enough; routes opt-in by passing the
    capability name to this factory.

    Tier lookup re-uses the cached score from B.3's rate-limit pass —
    no duplicate eternitas round-trip thanks to the TTL'd score_cache.
    """
    async def _dep(
        request: Request,
        response: Response,
        claims: PassportClaims = Depends(require_passport_with_eii_rate_limit),
    ) -> PassportClaims:
        settings = get_settings()
        redis = getattr(request.app.state, "redis", None)
        score_cache = getattr(request.app.state, "score_cache", None)

        # B.9.2 — scale the base cap by the passport's tier multiplier.
        # Default to 1.0× if score_cache isn't configured (matches the
        # B.3 fail-open posture).
        cap_multiplier = 1.0
        tier_name = "developing"
        if score_cache is not None:
            score = await score_cache.get(claims.passport)
            tier = tier_for_score(score)
            cap_multiplier = tier.cost_cap_multiplier
            tier_name = tier.name

        cap_usd = settings.monthly_cost_cap_usd_default * cap_multiplier
        decision = await cost_cap.charge(
            redis,
            passport=claims.passport,
            capability=capability,
            cap_usd=cap_usd,
            warning_pct=settings.monthly_cost_warning_pct,
        )

        # Stash for the route handler: (a) lets it report budget state in
        # the response BODY (headers alone never reach the fly's voice —
        # the agent-side notification wiring reads body fields), and
        # (b) lets it charge capability top-ups against the same cap
        # (e.g. web.browse when /web/fetch escalates to a Browserbase
        # render mid-handler, after this dependency already ran).
        request.state.cost_decision = decision
        request.state.cost_cap_usd = cap_usd
        request.state.cost_warning_pct = settings.monthly_cost_warning_pct

        per_usd = cost_cap.MICROCENTS_PER_USD
        response.headers["X-Cost-Cap-USD"] = f"{decision.cap_microcents / per_usd:.2f}"
        response.headers["X-Cost-Used-USD"] = f"{decision.used_after / per_usd:.6f}"
        response.headers["X-Cost-Capability"] = capability
        response.headers["X-Cost-Tier"] = tier_name
        response.headers["X-Cost-Tier-Multiplier"] = f"{cap_multiplier:g}"
        if decision.warning:
            response.headers["X-Cost-Warning"] = (
                f"Crossed {int(settings.monthly_cost_warning_pct * 100)}% of monthly budget"
            )

        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Monthly budget exhausted ({tier_name} tier cap "
                    f"${decision.cap_microcents / cost_cap.MICROCENTS_PER_USD:.2f}). "
                    f"Resets on the 1st."
                ),
                headers={
                    "Retry-After": "86400",
                    "X-Cost-Cap-USD": f"{decision.cap_microcents / per_usd:.2f}",
                    "X-Cost-Used-USD": f"{decision.used_after / per_usd:.6f}",
                    "X-Cost-Capability": capability,
                    "X-Cost-Tier": tier_name,
                },
            )

        return claims

    return _dep
