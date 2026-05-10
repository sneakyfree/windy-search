"""Canonical V1 wire-protocol types for Windy Search.

These types describe the public shape of `/v1/search` per the Windy Search
master plan §5 (`kit-army-config/docs/windy-search-master-plan-2026-05-10.md`).
They are the single agent-facing contract: agents call `search(query, ...)`
and receive uniform-shape `SearchResult` objects regardless of whether the
result came from the own-corpus index or a bridge (Brave, Google, domain
APIs). The router behind `/v1/search` (M1 work) is responsible for fan-out,
normalization, dedup, and ranking; this module only declares the shape.

Coexistence with existing `/web/search`: the legacy capability endpoint at
`app/web/search.py` keeps its own request/response shape until M1 router
work supersedes it. These types do not replace the legacy shape today; they
seed the canonical shape M1 builds against.

Per ADR-013 (marathon-stack) + master plan §4 strategic principle P1
(multi-bridge portfolio behind single agent-facing tool).
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class BridgeSource(StrEnum):
    """Where a `SearchResult` originated.

    The string value is the on-the-wire `_provenance.source` token that
    appears in `SearchResponse`. `OWN_CORPUS` is the index Windy Search
    operates itself; everything else is a bridge with a `bridge:` prefix
    so a quick string-prefix check distinguishes own vs external sourcing.

    Per master plan §4 P2: `OWN_CORPUS` share grows over 5-7 years from
    ~20% to ~95% of answered queries. The other values are not removed
    when their share shrinks — they remain available as a hedge per P1
    unwindability.
    """

    OWN_CORPUS = "own_corpus"
    BRAVE = "bridge:brave"
    GOOGLE = "bridge:google"
    MAPBOX = "bridge:mapbox"
    OSM = "bridge:osm"
    GITHUB = "bridge:github"
    STACKEXCHANGE = "bridge:stackexchange"
    SEMANTIC_SCHOLAR = "bridge:semantic_scholar"
    ARXIV = "bridge:arxiv"
    YOUTUBE = "bridge:youtube"


class Provenance(BaseModel):
    """Per-result origin metadata. Always present on `SearchResult`.

    Field shape varies by source:
      * `OWN_CORPUS` results carry `indexed_at`, `domain_ei`,
        `agent_friendliness_score`.
      * Bridge results carry `fetched_at` (when the bridge returned the
        result to us).

    Both shapes are valid; downstream consumers should treat unset fields
    as "not applicable to this source" rather than missing data.
    """

    source: BridgeSource
    indexed_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 timestamp when own-corpus indexed this URL. "
            "None for bridge results."
        ),
    )
    fetched_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 timestamp when this bridge response reached us. "
            "None for own-corpus results."
        ),
    )
    domain_ei: int | None = Field(
        default=None,
        ge=0,
        le=1000,
        description=(
            "Eternitas Integrity score for the result's domain. "
            "Own-corpus only at V1 (M5)."
        ),
    )
    agent_friendliness_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Per-domain agent-friendliness signal (M6). "
            "0=hostile, 1=ideal. Own-corpus only."
        ),
    )


class AgentContext(BaseModel):
    """Optional contextual hints from the calling agent.

    Used by the router to bias source selection (e.g., a `find_a_place`
    purpose biases toward Mapbox/OSM bridges). NOT used for tracking — per
    master plan §4 P6, no per-user history is retained by default.
    """

    purpose: str | None = Field(
        default=None,
        max_length=200,
        description="Free-form intent label from the calling agent. Used as a routing hint only.",
    )
    user_locale: str | None = Field(
        default=None,
        max_length=20,
        description=(
            "BCP-47 locale tag (e.g., 'en-US'). "
            "Used to bias geographic/language relevance."
        ),
    )


class SearchRequest(BaseModel):
    """The input to `POST /v1/search`."""

    query: str = Field(..., min_length=1, max_length=2000)
    max_results: int = Field(default=10, ge=1, le=50)
    agent_context: AgentContext | None = None


class SearchResult(BaseModel):
    """A single uniform-shape result returned to the calling agent.

    The agent sees only this shape. Whether the answer came from the own
    corpus or a bridge is visible via `_provenance.source` but is not
    expected to influence agent behavior — that's the router's job.
    """

    url: str = Field(..., min_length=1, max_length=2048)
    title: str = Field(..., max_length=500)
    snippet: str = Field(..., max_length=2000)
    rank: int = Field(..., ge=1, description="1-indexed final rank in the merged response.")
    provenance: Provenance = Field(
        ...,
        alias="_provenance",
        description=(
            "Origin metadata. Underscore-prefix on the wire signals "
            "'system-emitted, not agent-input'."
        ),
    )

    model_config = {"populate_by_name": True}


class SearchStats(BaseModel):
    """Aggregate counters for a single `/v1/search` call.

    Per master plan §4 P2 + §9, `bridges_used` is a load-bearing KPI —
    it's how the declining-bridge strategy is measured. The router emits
    this so observability dashboards can roll up `% of queries answered
    without bridge` over time.
    """

    own_corpus_results: int = Field(..., ge=0)
    bridge_results: int = Field(..., ge=0)
    bridges_used: list[BridgeSource] = Field(
        default_factory=list,
        description=(
            "Bridges that contributed to this response. "
            "Empty list = answered fully from own corpus."
        ),
    )
    ms_total: int = Field(..., ge=0, description="End-to-end latency in milliseconds.")


class SearchResponse(BaseModel):
    """The output of `POST /v1/search`."""

    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Opaque request id, prefix 'srch_'.",
    )
    results: list[SearchResult] = Field(default_factory=list)
    stats: SearchStats
