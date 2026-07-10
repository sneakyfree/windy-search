"""Browserbase render backend (B.6) — the Phase-1 rented browser layer.

Plain httpx (``fetch.py``) can't run JavaScript or get past bot-walls, so
JS-rendered SPAs come back to ``/web/fetch`` as near-empty shells. When
``BROWSERBASE_API_KEY`` is set, ``/web/fetch`` can escalate to Browserbase: a
real cloud Chrome navigates the URL and returns the *hydrated* DOM's visible
text.

Dormant when the key is unset (``is_configured()`` is False) — the same
posture as the Brave/Google search bridges. The eventual own-built
replacement is **Windy Hand** (``sneakyfree/windy-hand``); this module is the
rented Phase-1 slot behind Windy Search's stable fetch interface.

No ``BROWSERBASE_PROJECT_ID`` is required from config: the project is derived
from the API key at runtime (``GET /v1/projects``), so the key alone resolves
it — consistent with how every Browserbase SDK behaves.
"""

from __future__ import annotations

import logging

import httpx

from app.web.fetch import FetchResponse, validate_fetchable_url

logger = logging.getLogger(__name__)

_BB_API = "https://api.browserbase.com/v1"

# Signatures in the plain-fetch text that mean "blocked or an unhydrated SPA".
_CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript",
    "cf-browser-verification",
    "verifying you are human",
    "attention required",
    "captcha",
)


class BrowserbaseRenderer:
    """Renders a URL in a Browserbase cloud browser and returns visible text."""

    via = "browserbase"

    def __init__(self, api_key: str | None) -> None:
        self._api_key = api_key
        self._project_id: str | None = None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-BB-API-Key": self._api_key or "", "Content-Type": "application/json"}

    async def _project(self, client: httpx.AsyncClient) -> str:
        """Resolve (and cache) the project id from the API key."""
        if self._project_id:
            return self._project_id
        r = await client.get(f"{_BB_API}/projects", headers=self._headers())
        r.raise_for_status()
        projects = r.json()
        if not projects:
            raise RuntimeError("Browserbase account has no projects")
        self._project_id = projects[0]["id"]
        return self._project_id

    async def render(
        self, url: str, *, timeout_s: float = 30.0, ept: str | None = None
    ) -> FetchResponse:
        """Render ``url`` in a cloud browser; return its visible text as a
        ``FetchResponse`` (so the router handles it identically to a plain
        fetch). Raises ``RuntimeError`` on any failure — the caller maps that
        to a 502. Never call when ``is_configured()`` is False.

        ``ept`` is accepted for backend-interface parity (the Windy Hand
        backend forwards it); Browserbase has no use for it."""
        if not self.is_configured():
            raise RuntimeError("Browserbase not configured")
        # Same scheme/host safety posture as the plain fetch path.
        validate_fetchable_url(url)

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - deploy dependency
            raise RuntimeError(f"playwright not installed: {exc}") from exc

        async with httpx.AsyncClient(timeout=20.0) as client:
            pid = await self._project(client)
            resp = await client.post(
                f"{_BB_API}/sessions", headers=self._headers(), json={"projectId": pid}
            )
            resp.raise_for_status()
            sess = resp.json()
        connect_url = sess["connectUrl"]
        session_id = sess["id"]

        final_url = url
        status_code = 200
        text = ""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(connect_url)
                try:
                    ctx = (
                        browser.contexts[0]
                        if browser.contexts
                        else await browser.new_context()
                    )
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    nav = await page.goto(
                        url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000)
                    )
                    # Give SPA JS a beat to hydrate; a kept-open socket is fine.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    final_url = page.url
                    status_code = nav.status if nav else 200
                    text = await page.evaluate(
                        "document.body ? document.body.innerText : ''"
                    )
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning(
                "Browserbase render failed for %s (session %s): %s", url, session_id, exc
            )
            raise RuntimeError(f"render failed: {exc}") from exc

        text = (text or "").strip()
        return FetchResponse(
            final_url=final_url,
            status_code=status_code,
            content_type="text/plain; charset=utf-8",
            content=text,
            total_chars=len(text),
            offset=0,
            max_chars=len(text) or 1,
            truncated=False,
        )


def looks_like_needs_render(result: FetchResponse) -> bool:
    """Heuristic for ``render='auto'``: did the plain fetch likely miss
    JS-rendered content or hit a bot-wall? ``result.content`` is already the
    tag-stripped visible text, so a very short body ≈ an unhydrated SPA shell;
    a challenge signature ≈ a bot-wall."""
    body = result.content or ""
    low = body[:2000].lower()
    if any(marker in low for marker in _CHALLENGE_MARKERS):
        return True
    if len(body.strip()) < 300:
        return True
    return False
