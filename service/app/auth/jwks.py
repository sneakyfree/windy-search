"""JWKS fetcher with TTL cache.

Eternitas exposes its public signing keys at
`/.well-known/eternitas-keys` in standard JWKS format. We cache the
fetched key set in-process and refresh on TTL expiry. A `kid`-aware
fallback re-fetches when an unknown key id appears (covers post-rotation
windows where a freshly-issued EPT signs against a key we haven't pulled
yet).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


class JWKSCache:
    """Holds the fetched JWKS and refreshes it lazily on TTL expiry."""

    def __init__(
        self,
        jwks_url: str,
        ttl_seconds: int = 3600,
        http_timeout_seconds: float = 10.0,
    ) -> None:
        self.jwks_url = jwks_url
        self.ttl = ttl_seconds
        self.http_timeout = http_timeout_seconds
        self._cached: dict[str, Any] | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.get(self.jwks_url)
            resp.raise_for_status()
            return resp.json()

    async def get(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return the JWKS, fetching only when stale (or forced)."""
        now = time.time()
        if (
            not force_refresh
            and self._cached is not None
            and (now - self._fetched_at) < self.ttl
        ):
            return self._cached

        async with self._lock:
            # Re-check after acquiring the lock — a concurrent caller may
            # have already populated the cache.
            now = time.time()
            if (
                not force_refresh
                and self._cached is not None
                and (now - self._fetched_at) < self.ttl
            ):
                return self._cached

            self._cached = await self._fetch()
            self._fetched_at = time.time()
            return self._cached

    async def find_key(self, kid: str) -> dict[str, Any] | None:
        """Look up a key by `kid`. On miss, force-refresh once before
        returning None — covers the rotation window where a new key is in
        use upstream but we haven't fetched it yet."""
        jwks = await self.get()
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return key

        jwks = await self.get(force_refresh=True)
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return key
        return None
