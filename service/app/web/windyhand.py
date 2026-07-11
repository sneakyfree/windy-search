"""Windy Hand render backend — the Phase-2 OWN-BUILT browser fleet.

Occupies the same render slot `browserbase.py` (the Phase-1 rented
layer) holds today, per ADR-WH-001: separate runtime, unified
interface, swappable backend. When `WINDY_HAND_BASE_URL` is set, Windy
Search's `/web/fetch` escalations go to our own fleet (windy-hand's
`POST /render`) instead of a rented Browserbase session; when unset,
this backend is dormant (`is_configured()` False) — the exact
`is_configured()` posture of every other bridge.

Auth: the CALLER's EPT is forwarded verbatim, so windy-hand's own
EPT gate, per-passport rate limit, and web.render cost meter see the
true passport, and its integrity events attribute to the real agent.
(The two services' meters are reconciled in the router, not here:
`_settle_single_meter` refunds search's pessimistic web.browse charge
when this backend served the render, so the caller pays hand's
web.render only — never both.)

Honesty note (P8): windy-hand renders with an honest WindyHandBot user
agent and respects robots.txt as hard defaults. A robots-disallowed
page returns 403 from the fleet and surfaces here as a render failure —
that is intended behavior, not a bug to route around.
"""

from __future__ import annotations

import logging

import httpx

from app.web.fetch import FetchResponse, validate_fetchable_url

logger = logging.getLogger(__name__)


class WindyHandRenderer:
    """Renders a URL in the Windy Hand fleet and returns visible text."""

    via = "windy-hand"

    def __init__(self, base_url: str | None, timeout_seconds: float = 75.0) -> None:
        # timeout covers navigation + hydrate settle + queue wait on the
        # fleet side (its own hard cap is ~timeout_s + 18s).
        self._base_url = (base_url or "").rstrip("/") or None
        self._timeout = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._base_url)

    async def render(
        self, url: str, *, timeout_s: float = 30.0, ept: str | None = None
    ) -> FetchResponse:
        """Render ``url`` in the Windy Hand fleet; return its visible text
        as a ``FetchResponse`` (so the router handles it identically to a
        plain fetch). Raises ``RuntimeError`` on any failure — the caller
        maps that to a 502. Never call when ``is_configured()`` is False."""
        if not self.is_configured():
            raise RuntimeError("Windy Hand not configured")
        if not ept:
            raise RuntimeError("Windy Hand render requires the caller's EPT")
        # Same scheme/host safety posture as the plain fetch path (the
        # fleet re-validates navigation + every subresource itself).
        validate_fetchable_url(url)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/render",
                    headers={"Authorization": f"Bearer {ept}"},
                    json={"url": url, "timeout_s": timeout_s},
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"windy-hand unreachable: {type(exc).__name__}") from exc

        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))[:200]
            except Exception:  # noqa: BLE001 — body shape is best-effort
                detail = resp.text[:200]
            raise RuntimeError(f"windy-hand render {resp.status_code}: {detail}")

        body = resp.json()
        text = (body.get("text") or "").strip()
        return FetchResponse(
            final_url=body.get("final_url", url),
            status_code=int(body.get("status_code", 200)),
            content_type="text/plain; charset=utf-8",
            content=text,
            total_chars=len(text),
            offset=0,
            max_chars=len(text) or 1,
            truncated=False,
        )
