"""Merge utilities — anti-monoculture per-source cap (M1.7).

The cap prevents any single `Source` from dominating the merged list.
With `max_results=10` and the default 0.7 fraction, no single source
contributes more than 7 results. The remaining slots go to the next-
preferred sources, so the agent sees diversity rather than e.g. all
10 results from Brave when own-corpus or Google also had content.

The cap applies uniformly — own_corpus is NOT exempt, even though
master plan §4 P2 plans for own_corpus to dominate over time. The
cap is a guard against PATHOLOGICAL monoculture (a single source
returning 50 results while others starve); when the user actually
wants own-corpus to dominate, they ask via `max_results` and the
cap binds only on the long tail.

Per master plan §6 M1.7 + ADR-014.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from app.sources.base import RawResult, Source

DEFAULT_MAX_FRACTION = 0.7


def apply_per_source_cap(
    pairs: Sequence[tuple[Source, RawResult]],
    *,
    max_results: int,
    max_fraction: float = DEFAULT_MAX_FRACTION,
) -> list[tuple[Source, RawResult]]:
    """Limit per-source contribution to `max(2, ceil(max_results * max_fraction))`.

    Input order is preserved within each source — the cap drops trailing
    results, not random ones. Sources that haven't hit their cap yet are
    unaffected.

    Floor of 2 ensures small `max_results` (e.g., 3) doesn't compute a
    cap of 1 that would silently disable a source. `max_fraction=1.0`
    disables capping (a single source can fill the entire response).
    """
    cap = max(2, math.ceil(max_results * max_fraction))
    counts: dict[str, int] = {}
    out: list[tuple[Source, RawResult]] = []
    for source, raw in pairs:
        n = counts.get(source.name, 0)
        if n >= cap:
            continue
        counts[source.name] = n + 1
        out.append((source, raw))
    return out
