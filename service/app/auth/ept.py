"""Eternitas Passport Token (EPT) verification.

EPT is a JWT signed by eternitas with ES256 (P-256). Claim shape (from
`eternitas/services/credential_issuer.py:24-39`):

    sub: passport (ET-XXXX-XXXX)
    iss: "eternitas.ai"
    iat, exp: unix timestamps
    ope: operator_id
    bot: bot_name
    typ: bot_type (agent | other | ...)
    tru: legacy trust_score (0-100; deprecated, see master plan A.8)
    ver: verification_tier (e.g. "verified")
    rev: revocation flag (False on issuance)

This module verifies signature + standard claims. Revocation checking
(against the eternitas CRL) lands in a future codon — for B.2 the `rev`
claim flag is the only revocation signal honored.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import HTTPException

from app.auth.jwks import JWKSCache


@dataclass(frozen=True)
class PassportClaims:
    """Verified EPT claims surfaced to route handlers."""
    passport: str
    operator_id: str
    bot_name: str
    bot_type: str
    trust_score: int
    verification_tier: str
    issued_at: int
    expires_at: int


async def verify_ept(token: str, jwks_cache: JWKSCache) -> PassportClaims:
    """Verify an EPT and return its claims. Raises HTTPException(401) on
    any failure mode — malformed, unknown kid, bad signature, expired,
    wrong issuer, or revoked-flag set."""
    try:
        unverified = jwt.get_unverified_header(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed EPT")

    kid = unverified.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="EPT header missing 'kid'")

    key_dict = await jwks_cache.find_key(kid)
    if key_dict is None:
        raise HTTPException(status_code=401, detail=f"Unknown EPT signing key: {kid}")

    try:
        public_key = jwt.PyJWK(key_dict).key
    except Exception:
        raise HTTPException(status_code=401, detail="JWKS entry could not be loaded")

    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["ES256"],
            issuer="eternitas.ai",
            options={"verify_aud": False},  # eternitas EPTs don't set 'aud' yet
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="EPT expired")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="EPT issuer mismatch")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"EPT invalid: {e}")

    if claims.get("rev") is True:
        raise HTTPException(status_code=401, detail="EPT revoked")

    return PassportClaims(
        passport=claims["sub"],
        operator_id=claims.get("ope", ""),
        bot_name=claims.get("bot", ""),
        bot_type=claims.get("typ", ""),
        trust_score=int(claims.get("tru", 0)),
        verification_tier=claims.get("ver", ""),
        issued_at=int(claims.get("iat", 0)),
        expires_at=int(claims.get("exp", 0)),
    )
