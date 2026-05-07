"""Async HTTP client for the eternitas Integrity Event API.

This is the keystone — every Windy Search capability that completes
posts an integrity event to eternitas, closing the master-plan loop
where Phase A's gate first earns its keep.

Endpoint contract: POST /api/v1/integrity/events on api.eternitas.ai
  Auth:    X-API-Key: et_plt_*
  Idempotency: optional Idempotency-Key header
  Body:    {passport, event_type, dimension, delta_hint, source, context?}
  Returns: 201 with score_before/after + weighting block
           or 401/404/409/422

Best-effort posture: when the platform key isn't configured (B.11 deploy
hasn't happened yet) OR the eternitas call fails, we log + carry on.
The user-facing search/fetch result is what matters; the audit trail is
nice-to-have, never load-bearing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class EternitasClient:
    """Posts integrity events to eternitas. Stateless aside from config."""

    def __init__(
        self,
        base_url: str,
        platform_api_key: Optional[str],
        timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.platform_api_key = platform_api_key
        self.timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.platform_api_key)

    async def submit_integrity_event(
        self,
        *,
        passport: str,
        event_type: str,
        dimension: str,
        delta_hint: int,
        source: str,
        context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Submit one event. Returns parsed response on 201, None when
        the client isn't configured or the call fails (logged)."""
        if not self.configured:
            logger.debug(
                "Eternitas client not configured — skipping event %s/%s for %s",
                event_type, dimension, passport,
            )
            return None

        url = f"{self.base_url}/api/v1/integrity/events"
        headers: dict[str, str] = {
            "X-API-Key": self.platform_api_key,  # type: ignore[dict-item]
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        body: dict[str, Any] = {
            "passport": passport,
            "event_type": event_type,
            "dimension": dimension,
            "delta_hint": delta_hint,
            "source": source,
        }
        if context:
            body["context"] = context

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            logger.warning("Eternitas event post failed (network) for %s: %s", passport, e)
            return None

        if resp.status_code == 201:
            return resp.json()

        logger.warning(
            "Eternitas event post returned %d for %s: %s",
            resp.status_code, passport, resp.text[:200],
        )
        return None
