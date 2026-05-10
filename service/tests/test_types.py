"""Round-trip + shape tests for the canonical V1 wire-protocol types.

Validates the master-plan §5 wire shapes serialize and deserialize cleanly
and that the `_provenance` alias works on both sides. These tests pin the
public contract; if any field name or shape changes we want the test
diff to make that visible.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.types import (
    AgentContext,
    BridgeSource,
    Provenance,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchStats,
)


def test_bridge_source_wire_values() -> None:
    assert BridgeSource.OWN_CORPUS.value == "own_corpus"
    assert BridgeSource.BRAVE.value == "bridge:brave"
    assert BridgeSource.GOOGLE.value == "bridge:google"
    assert BridgeSource.MAPBOX.value == "bridge:mapbox"
    assert BridgeSource.OSM.value == "bridge:osm"
    assert BridgeSource.GITHUB.value == "bridge:github"
    assert BridgeSource.STACKEXCHANGE.value == "bridge:stackexchange"
    assert BridgeSource.SEMANTIC_SCHOLAR.value == "bridge:semantic_scholar"
    assert BridgeSource.ARXIV.value == "bridge:arxiv"
    assert BridgeSource.YOUTUBE.value == "bridge:youtube"


def test_provenance_own_corpus_round_trip() -> None:
    p = Provenance(
        source=BridgeSource.OWN_CORPUS,
        indexed_at="2026-05-09T14:32Z",
        domain_ei=720,
        agent_friendliness_score=0.91,
    )
    dumped = p.model_dump(mode="json")
    assert dumped["source"] == "own_corpus"
    assert dumped["indexed_at"] == "2026-05-09T14:32Z"
    assert dumped["domain_ei"] == 720
    assert dumped["agent_friendliness_score"] == 0.91
    assert dumped["fetched_at"] is None

    restored = Provenance.model_validate(dumped)
    assert restored == p


def test_provenance_bridge_round_trip() -> None:
    p = Provenance(source=BridgeSource.BRAVE, fetched_at="2026-05-10T18:15Z")
    dumped = p.model_dump(mode="json")
    assert dumped["source"] == "bridge:brave"
    assert dumped["fetched_at"] == "2026-05-10T18:15Z"
    assert dumped["indexed_at"] is None
    assert dumped["domain_ei"] is None
    assert dumped["agent_friendliness_score"] is None

    restored = Provenance.model_validate(dumped)
    assert restored == p


def test_provenance_score_range_validation() -> None:
    with pytest.raises(ValidationError):
        Provenance(source=BridgeSource.OWN_CORPUS, agent_friendliness_score=1.5)
    with pytest.raises(ValidationError):
        Provenance(source=BridgeSource.OWN_CORPUS, domain_ei=1500)
    with pytest.raises(ValidationError):
        Provenance(source=BridgeSource.OWN_CORPUS, domain_ei=-5)


def test_search_request_defaults_and_bounds() -> None:
    req = SearchRequest(query="warranty terms 2019 Whirlpool dishwasher")
    assert req.max_results == 10
    assert req.agent_context is None

    with pytest.raises(ValidationError):
        SearchRequest(query="")
    with pytest.raises(ValidationError):
        SearchRequest(query="x", max_results=0)
    with pytest.raises(ValidationError):
        SearchRequest(query="x", max_results=51)


def test_search_request_with_agent_context() -> None:
    req = SearchRequest(
        query="austin tx coffee shops",
        max_results=5,
        agent_context=AgentContext(purpose="answer_user_question", user_locale="en-US"),
    )
    assert req.agent_context is not None
    assert req.agent_context.purpose == "answer_user_question"
    assert req.agent_context.user_locale == "en-US"


def test_search_result_provenance_alias_round_trip() -> None:
    """`_provenance` is the on-the-wire field name; `provenance` is the
    Python attribute name. Both must round-trip cleanly."""
    payload = {
        "url": "https://example.com/whirlpool-warranty",
        "title": "Whirlpool dishwasher warranty",
        "snippet": "Standard 1-year warranty applies...",
        "rank": 1,
        "_provenance": {
            "source": "own_corpus",
            "indexed_at": "2026-05-09T14:32Z",
            "domain_ei": 720,
            "agent_friendliness_score": 0.91,
        },
    }
    result = SearchResult.model_validate(payload)
    assert result.provenance.source == BridgeSource.OWN_CORPUS
    assert result.rank == 1

    dumped = result.model_dump(mode="json", by_alias=True)
    assert "_provenance" in dumped
    assert "provenance" not in dumped
    assert dumped["_provenance"]["source"] == "own_corpus"


def test_search_response_full_round_trip() -> None:
    """Mirrors the master plan §5 wire example end-to-end."""
    payload = {
        "id": "srch_8f3a2b",
        "results": [
            {
                "url": "https://example.com/a",
                "title": "Result A",
                "snippet": "Snippet A",
                "rank": 1,
                "_provenance": {
                    "source": "own_corpus",
                    "indexed_at": "2026-05-09T14:32Z",
                    "domain_ei": 720,
                    "agent_friendliness_score": 0.91,
                },
            },
            {
                "url": "https://example.com/b",
                "title": "Result B",
                "snippet": "Snippet B",
                "rank": 2,
                "_provenance": {
                    "source": "bridge:brave",
                    "fetched_at": "2026-05-10T18:15Z",
                },
            },
        ],
        "stats": {
            "own_corpus_results": 1,
            "bridge_results": 1,
            "bridges_used": ["bridge:brave"],
            "ms_total": 287,
        },
    }
    response = SearchResponse.model_validate(payload)
    assert response.id == "srch_8f3a2b"
    assert len(response.results) == 2
    assert response.results[0].provenance.source == BridgeSource.OWN_CORPUS
    assert response.results[1].provenance.source == BridgeSource.BRAVE
    assert response.stats.bridges_used == [BridgeSource.BRAVE]
    assert response.stats.ms_total == 287

    dumped = response.model_dump(mode="json", by_alias=True)
    assert dumped["stats"]["bridges_used"] == ["bridge:brave"]


def test_search_stats_empty_bridges_means_full_own_corpus() -> None:
    """The declining-bridge KPI per master plan §4 P2 reads `bridges_used`
    to count own-corpus-only responses. An empty list must be the canonical
    'answered without bridge' signal."""
    stats = SearchStats(own_corpus_results=10, bridge_results=0, ms_total=42)
    assert stats.bridges_used == []
    dumped = stats.model_dump(mode="json")
    assert dumped["bridges_used"] == []


def test_search_result_bounds() -> None:
    base = {
        "url": "https://example.com",
        "title": "T",
        "snippet": "S",
        "_provenance": {"source": "own_corpus"},
    }
    with pytest.raises(ValidationError):
        SearchResult.model_validate({**base, "rank": 0})
    with pytest.raises(ValidationError):
        SearchResult.model_validate({**base, "rank": -1})
