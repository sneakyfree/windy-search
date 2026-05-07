"""EII score → tier mapping (Phase B.3 + B.9.2).

Tiers ladder from "critical" up to "exceptional"; each carries:
  - a per-passport requests-per-minute ceiling (B.3 rate limit)
  - a multiplier on the per-passport monthly USD cost cap (B.9.2)

Scores from `GET /api/v1/registry/{passport}/integrity` on eternitas.
Master plan anchors:

    Exceptional (900+)  → 200 req/min  ×10 cost cap
    Critical    (<400)  →   5 req/min  ×0.1 cost cap

The cap multiplier rewards reputation: a long-trusted passport with
EII 950 gets $50/month at the default $5 base, while a low-trust
passport at EII 300 gets only $0.50 — limiting blast radius from
new or score-degraded agents while letting good actors do real work.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    name: str
    floor: int  # inclusive lower bound on EII overall score
    requests_per_minute: int
    cost_cap_multiplier: float  # multiplies the base monthly_cost_cap_usd


# Highest-score tiers first — `tier_for_score` walks top-down and
# returns the first whose floor the score clears.
TIERS: tuple[Tier, ...] = (
    Tier("exceptional", 900, 200, 10.0),
    Tier("trusted",     700, 100,  5.0),
    Tier("developing",  500,  50,  1.0),  # baseline — matches the legacy default
    Tier("watch",       400,  20,  0.4),
    Tier("critical",      0,   5,  0.1),
)


def tier_for_score(score: int) -> Tier:
    """Return the tier whose floor the score clears. Defensive on
    out-of-range scores: negative → critical; values past 1000 still
    fall into exceptional rather than raising."""
    for t in TIERS:
        if score >= t.floor:
            return t
    return TIERS[-1]
