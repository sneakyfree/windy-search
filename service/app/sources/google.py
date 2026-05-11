"""Google Custom Search bridge — LAST-RESORT external bridge.

Per master plan §4 P1: Brave is the primary external bridge; Google is
the last-resort fallback. `priority=30` reflects that — the router
prefers any other configured source before sending traffic to Google.

Configuration requires BOTH:
  * `settings.google_search_api_key` — Custom Search JSON API key
  * `settings.google_cse_id`         — Custom Search Engine ID

When either is missing, `is_configured()` returns False and the router
skips this source. Same fail-open posture as the Brave adapter.

Per master plan §6 M2.2 + ADR-014.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from app.sources.base import RawResult, Source
from app.types import BridgeSource

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"
DEFAULT_TIMEOUT_S = 8.0
# Google Custom Search caps `num` at 10 per call.
GOOGLE_MAX_NUM = 10


class GoogleSource(Source):
    """Google Custom Search JSON API adapter."""

    def __init__(
        self,
        api_key: str | None,
        cse_id: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key
        self._cse_id = cse_id
        self._transport = transport
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return "google"

    @property
    def source_enum(self) -> BridgeSource:
        return BridgeSource.GOOGLE

    @property
    def priority(self) -> int:
        return 30  # last-resort per master plan §4 P1

    @property
    def cost_per_query(self) -> Decimal:
        # Google's Custom Search JSON API standard tier ≈ $5 / 1000 queries.
        return Decimal("0.005")

    def is_configured(self) -> bool:
        return bool(self._api_key and self._cse_id)

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        """Issue a single Custom Search call and normalize the response.

        Returns [] when the source isn't configured. May raise on
        transport failure — the router's `_safe_search` catches and
        logs.
        """
        if not self.is_configured():
            return []

        requested = int(opts.get("max_results", 10))
        num = max(1, min(requested, GOOGLE_MAX_NUM))

        params = {
            "q": query,
            "key": self._api_key,
            "cx": self._cse_id,
            "num": num,
        }

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            resp = await client.get(GOOGLE_SEARCH_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        items = data.get("items", []) or []
        return [
            RawResult(
                url=item.get("link", ""),
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
                source_rank=i,
            )
            for i, item in enumerate(items[:num], start=1)
        ]
