"""TTL-cached lookup of EII scores via eternitas.

The rate-limiter checks every authenticated request, so we avoid
hammering eternitas for every search by caching scores in-process for
~5 minutes. Stale-but-bounded data is acceptable here: when a passport
crosses a tier boundary, the new tier kicks in within the cache TTL.

Future codon (A.4 ↔ B.x): subscribe to the `integrity.event` webhook so
score changes invalidate the cache eagerly. That tightens the staleness
window to seconds without polling.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

NEUTRAL_SCORE = 500  # the eternitas baseline used when a passport is unknown


class IntegrityScoreCache:
    """In-memory TTL cache keyed by passport.

    Returns the eternitas neutral score (500) on 404 — better to keep an
    unknown agent at neutral and let normal rate limits apply than to
    deny outright. Truly bad passports are revoked at the eternitas
    layer; B.2 catches those via the EPT `rev` flag.
    """

    def __init__(
        self,
        eternitas_base_url: str,
        ttl_seconds: int = 300,
        http_timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = eternitas_base_url.rstrip("/")
        self.ttl = ttl_seconds
        self.http_timeout = http_timeout_seconds
        # passport → (score, fetched_at_unix)
        self._cache: dict[str, tuple[int, float]] = {}
        self._inflight: dict[str, asyncio.Future[int]] = {}

    def _is_fresh(self, fetched_at: float) -> bool:
        return (time.time() - fetched_at) < self.ttl

    async def _fetch(self, passport: str) -> int:
        url = f"{self.base_url}/api/v1/registry/{passport}/integrity"
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.get(url)

        if resp.status_code == 404:
            return NEUTRAL_SCORE
        resp.raise_for_status()
        body = resp.json()
        return int(body.get("overall", NEUTRAL_SCORE))

    async def get(self, passport: str) -> int:
        cached = self._cache.get(passport)
        if cached is not None and self._is_fresh(cached[1]):
            return cached[0]

        # Coalesce concurrent fetches for the same passport so a thundering
        # herd at cache-miss doesn't multiply eternitas load.
        existing = self._inflight.get(passport)
        if existing is not None:
            return await existing

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[int] = loop.create_future()
        self._inflight[passport] = fut
        try:
            try:
                score = await self._fetch(passport)
            except Exception as e:
                # Fail open at neutral — eternitas hiccups must not break
                # the whole gated surface. Logged for ops visibility.
                logger.warning("EII fetch failed for %s: %s", passport, e)
                score = NEUTRAL_SCORE
            self._cache[passport] = (score, time.time())
            fut.set_result(score)
            return score
        finally:
            self._inflight.pop(passport, None)

    def invalidate(self, passport: str | None = None) -> None:
        """Drop a single passport (or the entire cache when None). The
        webhook subscriber will call this on `integrity.event`."""
        if passport is None:
            self._cache.clear()
        else:
            self._cache.pop(passport, None)
