"""FastAPI dependency for EPT-gated routes."""

from fastapi import Header, HTTPException, Request

from app.auth.ept import PassportClaims, verify_ept


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
