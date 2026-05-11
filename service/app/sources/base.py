"""Abstract base for pluggable search sources.

The wire-protocol enum naming a source (`app.types.BridgeSource`) and the
runtime class implementing a source (`Source`) are deliberately decoupled:
the enum is the immutable contract written into responses, while `Source`
is the swappable runtime. Each concrete `Source.source_enum` returns the
matching enum value so normalization can stamp provenance.

M1 only ships stub implementations (`app.sources.stubs`); M2 brings the
first real bridge (Brave); M3 brings the own-corpus reader.

Per master plan §6 M1.3 — the verbal name there is "BridgeSource ABC."
The class here is `Source` to avoid colliding with the `StrEnum` named
`BridgeSource` in `app.types`. Otherwise the semantics match exactly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.types import BridgeSource


@dataclass(frozen=True, slots=True)
class RawResult:
    """A source's pre-normalization output. The router transforms this
    into the canonical `app.types.SearchResult` via `app.normalization`.

    `source_rank` is the original 1-indexed rank within this source's
    response. The router uses it as a tiebreaker when merging across
    sources of equal priority.
    """

    url: str
    title: str
    snippet: str
    source_rank: int | None = None


class Source(ABC):
    """Pluggable interface every bridge / own-corpus reader implements."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier (lowercase). Used in logs + telemetry."""

    @property
    @abstractmethod
    def source_enum(self) -> BridgeSource:
        """Wire-protocol enum value stamped on results from this source."""

    @property
    @abstractmethod
    def priority(self) -> int:
        """Lower = preferred. Router prefers lower-priority sources when
        budgets force a cut.

        Convention:
            own_corpus = 0   (always first per master plan §4 P2)
            primary    = 10  (e.g., Brave)
            secondary  = 20
            fallback   = 30  (e.g., Google — last-resort per §4 P1)
        """

    @property
    @abstractmethod
    def cost_per_query(self) -> Decimal:
        """Marginal USD per single-query call. 0 for own corpus, ~0.005
        for Brave, etc. M2+ cost-cap accounting reads this."""

    def is_configured(self) -> bool:
        """Default: assume configured. Bridges with API keys override to
        return False when the key is missing — the router skips them."""
        return True

    @abstractmethod
    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        """Issue the query.

        Must NOT raise on a no-results outcome — return an empty list.
        May raise on transport failure; the router catches and logs.
        Per-call options (e.g., `max_results`) flow through `**opts`.
        """
