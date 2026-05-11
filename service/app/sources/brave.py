"""Brave Search API adapter — primary external bridge per master plan §4 P1.

Replaces `app.sources.stubs.StubBraveSource` for production routing. The
stub stays in the tree because it remains useful for unit tests of
downstream modules (router/ranker/normalization) that don't want to
mock httpx.

Configured via `settings.brave_search_api_key`. When the key is unset,
`is_configured()` returns False and the router skips this source —
matches the same fail-open posture as the existing B.4 legacy code at
`app/web/search.py`.

Per master plan §6 M2.1 + ADR-014.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from app.sources.base import RawResult, Source
from app.types import BridgeSource

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_TIMEOUT_S = 8.0
# Brave's single-call count parameter is bounded to [1, 20].
BRAVE_MAX_COUNT = 20


class BraveSource(Source):
    """Brave Search API adapter.

    Tests inject `transport=httpx.MockTransport(...)` to avoid real
    network calls. Production passes `transport=None` so httpx uses
    its default async transport.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return "brave"

    @property
    def source_enum(self) -> BridgeSource:
        return BridgeSource.BRAVE

    @property
    def priority(self) -> int:
        return 10  # primary external bridge per master plan §4 P1

    @property
    def cost_per_query(self) -> Decimal:
        # Brave standard tier ≈ $5 / 1000 queries.
        return Decimal("0.005")

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        """Issue a single Brave search and normalize the response.

        Returns `[]` when the source isn't configured. May raise on
        transport failure — the router's `_safe_search` catches and
        logs.
        """
        if not self._api_key:
            return []

        requested = int(opts.get("max_results", 10))
        count = max(1, min(requested, BRAVE_MAX_COUNT))

        params = {"q": query, "count": count}
        headers = {
            "X-Subscription-Token": self._api_key,
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            resp = await client.get(BRAVE_SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        web_results = data.get("web", {}).get("results", []) or []
        return [
            RawResult(
                url=item.get("url", ""),
                title=item.get("title", ""),
                snippet=item.get("description", ""),
                source_rank=i,
            )
            for i, item in enumerate(web_results[:count], start=1)
        ]
