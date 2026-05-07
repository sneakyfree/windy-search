"""EII score → rate-limit tier mapping (Phase B.3).

Tiers ladder from "critical" up to "exceptional"; each provides a
per-passport requests-per-minute ceiling on Windy Search's gated
endpoints. Scores from `GET /api/v1/registry/{passport}/integrity`
on eternitas; the master plan calls for these specific anchors:

    Exceptional (900+)  → 200 req/min
    Critical    (<400)  →   5 req/min

The mid-tiers fill in linearly so behavior degrades gracefully as a
passport's reputation drops.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    name: str
    floor: int  # inclusive lower bound on EII overall score
    requests_per_minute: int


# Highest-score tiers first — `tier_for_score` walks top-down and
# returns the first whose floor the score clears.
TIERS: tuple[Tier, ...] = (
    Tier("exceptional", 900, 200),
    Tier("trusted", 700, 100),
    Tier("developing", 500, 50),
    Tier("watch", 400, 20),
    Tier("critical", 0, 5),
)


def tier_for_score(score: int) -> Tier:
    """Return the tier whose floor the score clears. Defensive on
    out-of-range scores: negative → critical; values past 1000 still
    fall into exceptional rather than raising."""
    for t in TIERS:
        if score >= t.floor:
            return t
    return TIERS[-1]
