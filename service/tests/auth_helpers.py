"""Test helpers — generate ES256 key pair, sign test EPTs, build JWKS."""

from __future__ import annotations

import base64
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def generate_ept_keypair(kid: str = "test-key-1") -> dict[str, Any]:
    """Build everything needed to sign + verify a test EPT.

    Returns a dict with:
        kid: str
        private_pem: bytes (sign with this)
        jwks: dict (return from a stub JWKSCache.find_key)
    """
    private = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private.public_key().public_numbers()
    x_bytes = public_numbers.x.to_bytes(32, "big")
    y_bytes = public_numbers.y.to_bytes(32, "big")

    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(x_bytes),
        "y": _b64url(y_bytes),
        "kid": kid,
        "use": "sig",
        "alg": "ES256",
    }

    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    return {
        "kid": kid,
        "private_pem": private_pem,
        "jwks": {"keys": [jwk]},
    }


def sign_test_ept(
    keypair: dict[str, Any],
    *,
    passport: str = "ET26-TEST-AAAA",
    operator_id: str = "op_test123",
    bot_name: str = "Test Bot",
    bot_type: str = "agent",
    trust_score: int = 80,
    verification_tier: str = "verified",
    expires_in: int = 3600,
    revoked: bool = False,
    issuer: str = "eternitas.ai",
) -> str:
    """Sign a test EPT mirroring the eternitas claim shape."""
    now = int(time.time())
    payload = {
        "sub": passport,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
        "ope": operator_id,
        "bot": bot_name,
        "typ": bot_type,
        "tru": trust_score,
        "ver": verification_tier,
        "rev": revoked,
    }
    headers = {"kid": keypair["kid"], "typ": "EPT"}
    return jwt.encode(
        payload,
        keypair["private_pem"],
        algorithm="ES256",
        headers=headers,
    )


class StubJWKSCache:
    """Drop-in replacement for app.auth.jwks.JWKSCache that returns a
    hardcoded JWKS — no network."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        self._jwks = jwks

    async def get(self, *, force_refresh: bool = False) -> dict[str, Any]:
        return self._jwks

    async def find_key(self, kid: str) -> dict[str, Any] | None:
        for key in self._jwks.get("keys", []):
            if key.get("kid") == kid:
                return key
        return None
