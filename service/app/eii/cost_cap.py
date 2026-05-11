"""Per-passport monthly cost cap (Phase B.9).

Backend APIs cost real money: Brave is ~$0.005/query, Browserbase is
~$0.05/session, Claude vision is ~$0.02/extraction. Without a budget
ceiling, a passport with low-tier rate limits can still rack up
hundreds of dollars by patient grinding (5/min × 60 × 24 × 30 = 216K
requests/month even at the worst tier).

This codon adds a per-passport USD ceiling that resets at the start
of each calendar month. Storage is one Redis key per (passport,
YYYY-MM), atomic INCR. Spend is tracked in **microcents** (10^-6 USD)
to keep INCR semantics integer across all the capability cost units.

Capability costs are catalogued in COSTS — the canonical source. Add
a new capability there when its codon lands.

Default cap: $5/month from settings.monthly_cost_cap_usd_default.
Future codons can per-tier this (Exceptional gets $50, Critical gets
$1) by mapping tier → cap in tiers.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# 10^6 microcents = 1 USD. Lets us track $0.000001 increments without
# floating-point arithmetic. Brave at $5/1000 = 500 microcents/query.
MICROCENTS_PER_USD = 1_000_000

# Cost catalog. Update when a new capability lands.
#
# Notes:
#   web.search   — Brave Search API, $5 per 1000 queries
#   web.fetch    — bandwidth + httpx; effectively free per request
#   web.browse   — Browserbase ~$0.05/session (B.6)
#   web.extract  — Claude Haiku ~$0.02/extraction (B.7)
#   web.research — sum of components, billed once at completion (B.8)
COSTS: dict[str, int] = {
    "web.search":  500,        # $0.005
    "web.fetch":   1,          # $0.000001
    "web.browse":  50_000,     # $0.05
    "web.extract": 20_000,     # $0.02
    "web.research": 75_000,    # $0.075 — search + fetch + extract typical
    # M2.4 — /v1/search fans out to up to 2 paid bridges (Brave + Google
    # at $0.005 each). Charge pessimistically; the route handler refunds
    # when `bridges_used` is empty (answered fully from own corpus).
    "v1.search":   1_000,      # $0.01
}

KEY_PREFIX = "cost:windy-search:passport"


@dataclass(frozen=True)
class CostDecision:
    allowed: bool
    cap_microcents: int
    used_before: int
    used_after: int
    warning: bool        # True once spend crosses the configured warning threshold
    capability: str
    cost_charged: int


def _current_month() -> str:
    """YYYY-MM in UTC. Calendar boundary, not rolling 30-day — simpler
    operationally; agents see their budget reset on the 1st."""
    return datetime.now(UTC).strftime("%Y-%m")


def _key(passport: str) -> str:
    return f"{KEY_PREFIX}:{passport}:{_current_month()}"


async def charge(
    redis: aioredis.Redis | None,
    passport: str,
    capability: str,
    cap_usd: float,
    warning_pct: float,
) -> CostDecision:
    """Charge the passport's monthly budget for one call to `capability`.

    Returns a CostDecision describing the outcome. When `allowed=False`
    the route should 429 the request — the budget is already exhausted.
    Atomic: if the INCRBY puts us over the cap, the next caller still
    sees `used_before > cap_microcents` and gets denied; the only "loss"
    is the request that pushed across the line. Acceptable.

    Fails open when redis is None — same posture as rate_limit.
    """
    cost = COSTS.get(capability, 0)
    cap_microcents = int(cap_usd * MICROCENTS_PER_USD)

    if redis is None:
        return CostDecision(
            allowed=True,
            cap_microcents=cap_microcents,
            used_before=0,
            used_after=cost,
            warning=False,
            capability=capability,
            cost_charged=cost,
        )

    key = _key(passport)
    try:
        used_after = int(await redis.incrby(key, cost))
        # Tag the key with a 35-day TTL so stale months drop on their own
        # without a sweeper. 35 covers any edge cases around long months
        # (Jan, Mar, May, Jul, Aug, Oct, Dec are all 31).
        await redis.expire(key, 35 * 86400)
    except Exception as e:
        logger.warning("cost cap INCRBY failed for %s: %s", passport, e)
        return CostDecision(
            allowed=True,
            cap_microcents=cap_microcents,
            used_before=0,
            used_after=cost,
            warning=False,
            capability=capability,
            cost_charged=cost,
        )

    used_before = used_after - cost
    allowed = used_before < cap_microcents  # gate on the *pre-charge* state
    threshold = int(cap_microcents * warning_pct)
    warning = used_after >= threshold and used_before < threshold

    if not allowed:
        # Roll the charge back so we don't keep bumping a depleted budget
        # past the cap (purely cosmetic; doesn't affect the deny decision).
        try:
            await redis.incrby(key, -cost)
            used_after -= cost
        except Exception:
            pass

    return CostDecision(
        allowed=allowed,
        cap_microcents=cap_microcents,
        used_before=used_before,
        used_after=used_after,
        warning=warning,
        capability=capability,
        cost_charged=cost if allowed else 0,
    )


async def refund(
    redis: aioredis.Redis | None,
    passport: str,
    capability: str,
) -> int:
    """Refund a previously-charged cost. Used by B.10 on cache hit so the
    monthly counter reflects real backend spend, not theoretical.

    Returns the new accumulated total after refund (informational; not
    surfaced in headers since the cost-cap response headers reflect the
    pre-refund state, which is fine — caller knows from the cache_hit
    flag in the response body).
    """
    if redis is None:
        return 0
    cost = COSTS.get(capability, 0)
    if cost == 0:
        return 0
    key = _key(passport)
    try:
        return int(await redis.incrby(key, -cost))
    except Exception as e:
        logger.warning("cost refund failed for %s/%s: %s", passport, capability, e)
        return 0
