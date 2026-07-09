"""Passport revocation enforcement — CRL cache + webhook blacklist.

The EPT is a long-lived (365-day) bearer token verified fully offline;
its `rev` claim is baked at mint time, so a revocation issued AFTER the
token was minted is invisible to signature verification. This module
closes that hole with two complementary signals:

  1. CRL cache — eternitas publishes every revoked passport at
     `/.well-known/eternitas-crl` (updates in <1s, shape
     `{"updated_at": ..., "revoked": [{"passport": ..., ...}]}`).
     We cache the set in-process for `ttl_seconds` (default 30s) and
     consult it on every authenticated request, so a revoked passport
     is rejected within one TTL window at worst — even if the webhook
     delivery was missed (e.g. during a deploy).
  2. Webhook blacklist — `passport.revoked` / `passport.suspended`
     firehose events (see app/webhooks/consumer.py) blacklist the
     passport immediately: rejection in seconds, not a TTL window.
     Revocations are permanent for the process lifetime (the CRL is the
     durable source across restarts); suspensions are reversible, so
     those entries expire after `suspended_ttl_seconds` and re-arm on
     each delivery while the suspension stands.

Failure semantics (ADR-026 §4 — graceful gates):
  - CRL reachable → refresh, decide.
  - CRL unreachable + cache younger than `max_stale_seconds` → serve
    stale. (No new revocation can originate while eternitas is down,
    so a bounded stale window loses nothing.)
  - CRL unreachable beyond `max_stale_seconds` (or never fetched) →
    fail CLOSED when `fail_closed` (production): 503 on gated routes.
    A search service that fails open when eternitas dies would
    recreate the 365-day hole this module exists to close.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)


class RevocationCache:
    """In-process revocation state: TTL-cached CRL + webhook blacklist."""

    def __init__(
        self,
        crl_url: str,
        ttl_seconds: int = 30,
        max_stale_seconds: int = 300,
        fail_closed: bool = True,
        suspended_ttl_seconds: int = 3600,
        http_timeout_seconds: float = 5.0,
    ) -> None:
        self.crl_url = crl_url
        self.ttl = ttl_seconds
        self.max_stale = max_stale_seconds
        self.fail_closed = fail_closed
        self.suspended_ttl = suspended_ttl_seconds
        self.http_timeout = http_timeout_seconds
        self._revoked: frozenset[str] = frozenset()
        self._fetched_at: float = 0.0  # 0.0 = never fetched successfully
        self._webhook_revoked: set[str] = set()
        self._webhook_suspended: dict[str, float] = {}  # passport → blacklisted_at
        self._lock = asyncio.Lock()

    # ---- webhook path (called by webhooks/consumer.py) ----------------

    def blacklist(self, passport: str, *, suspended: bool = False) -> None:
        if suspended:
            self._webhook_suspended[passport] = time.time()
        else:
            self._webhook_revoked.add(passport)

    # ---- CRL path ------------------------------------------------------

    async def _fetch(self) -> frozenset[str]:
        async with httpx.AsyncClient(timeout=self.http_timeout) as client:
            resp = await client.get(self.crl_url)
            resp.raise_for_status()
            body = resp.json()
        return frozenset(
            entry["passport"]
            for entry in body.get("revoked", [])
            if isinstance(entry, dict) and entry.get("passport")
        )

    async def _refresh_if_stale(self) -> None:
        """Refresh the CRL when past TTL. Raises HTTPException(503) only
        when the CRL is unreachable beyond the stale allowance and the
        cache is configured fail-closed."""
        now = time.time()
        if self._fetched_at and (now - self._fetched_at) < self.ttl:
            return

        async with self._lock:
            now = time.time()
            if self._fetched_at and (now - self._fetched_at) < self.ttl:
                return  # a concurrent caller refreshed while we waited
            try:
                self._revoked = await self._fetch()
                self._fetched_at = time.time()
                return
            except Exception as e:
                age = (now - self._fetched_at) if self._fetched_at else None
                if age is not None and age < self.max_stale:
                    logger.warning(
                        "CRL refresh failed (%s); serving %.0fs-stale CRL "
                        "(max_stale=%ds)", e, age, self.max_stale,
                    )
                    return
                if not self.fail_closed:
                    logger.error(
                        "CRL unreachable past max_stale (%s) — failing OPEN "
                        "(non-production posture)", e,
                    )
                    return
                logger.error(
                    "CRL unreachable past max_stale (%s) — failing CLOSED", e,
                )
                raise HTTPException(
                    status_code=503,
                    detail="Revocation status unavailable — retry shortly",
                )

    # ---- the gate check -------------------------------------------------

    async def check(self, passport: str) -> None:
        """Raise HTTPException(401) if the passport is revoked/suspended,
        HTTPException(503) if revocation state is unavailable fail-closed.
        Returns None when the passport is clear."""
        if passport in self._webhook_revoked:
            raise HTTPException(status_code=401, detail="EPT revoked")

        suspended_at = self._webhook_suspended.get(passport)
        if suspended_at is not None:
            if (time.time() - suspended_at) < self.suspended_ttl:
                raise HTTPException(status_code=401, detail="EPT suspended")
            del self._webhook_suspended[passport]

        await self._refresh_if_stale()

        if passport in self._revoked:
            raise HTTPException(status_code=401, detail="EPT revoked")
