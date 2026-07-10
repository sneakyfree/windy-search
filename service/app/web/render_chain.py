"""Ordered failover chain for the render slot (ADR-WH-001).

The render slot is not one backend — it's a priority-ordered list. A
render tries each configured backend in turn and transparently falls
through to the next on failure, so one backend having a bad day never
reaches the user ("default to our own native; pivot fast to Browserbase
so users don't get caught").

`RENDER_BACKENDS` (comma-separated, config.py) sets the order; only names
whose backend is `is_configured()` participate. Today's default —
`browserbase` alone — is exactly the pre-failover behavior. The
push-button flip to `windy-hand,browserbase` makes our own fleet primary
with Browserbase as the automatic safety net.

Each backend exposes `is_configured()`, `via` (its tag), and
`render(url, *, ept=...)`. This module is a pure function over the chain —
no shared mutable state, safe under concurrency.
"""

from __future__ import annotations

import logging

from app.web.fetch import FetchResponse

logger = logging.getLogger(__name__)


def build_chain(order: list[str], by_name: dict[str, object]) -> list[object]:
    """Resolve backend names to instances, preserving order and dropping
    unknown names. Unconfigured backends are kept in the list (they're
    skipped at render time) so a later-configured one still works."""
    chain: list[object] = []
    for name in order:
        b = by_name.get(name.strip())
        if b is not None:
            chain.append(b)
        elif name.strip():
            logger.warning("unknown render backend %r in RENDER_BACKENDS", name)
    return chain


def chain_has_configured(chain: list[object]) -> bool:
    return any(getattr(b, "is_configured", lambda: False)() for b in chain)


class AllBackendsFailedError(RuntimeError):
    """Every configured backend in the chain failed to render."""


async def render_with_failover(
    chain: list[object], url: str, *, ept: str | None
) -> tuple[FetchResponse, str, list[str]]:
    """Try each configured backend in priority order. Returns
    (response, served_via, fallbacks_used) where fallbacks_used lists the
    backends that failed before one succeeded (empty on a first-try win).
    Raises AllBackendsFailedError if every configured backend fails."""
    errors: list[str] = []
    tried: list[str] = []
    for b in chain:
        if not getattr(b, "is_configured", lambda: False)():
            continue
        via = getattr(b, "via", "unknown")
        try:
            resp = await b.render(url, ept=ept)
            return resp, via, tried
        except Exception as e:  # noqa: BLE001 — try the next backend
            tried.append(via)
            errors.append(f"{via}: {type(e).__name__}")
            logger.warning(
                "render backend %s failed for %s, falling through: %s", via, url, e
            )
    raise AllBackendsFailedError(
        "all render backends failed (" + "; ".join(errors) + ")"
        if errors
        else "no render backend configured"
    )
