"""Unit tests for app/ranking.py (M1.5).

Covers:
  * `rank` — BM25 prefers docs with more query-term coverage; source
    weight breaks ties between sources with equal content scores;
    empty input + empty query are no-ops; stable tiebreak.
  * `source_weight` — own_corpus(0)=1.0, brave(10)=0.5, google(30)=0.25.
  * `_tokenize` — case folding + non-alphanumeric splitting.

Uses crafted RawResults with distinct content rather than stub-fixture
results — the stubs all carry the query verbatim in every snippet,
which makes BM25 scores effectively tied and obscures the ranker's
relevance signal.
"""
from __future__ import annotations

from app.ranking import (
    K1,
    B,
    _tokenize,
    rank,
    source_weight,
)
from app.sources.base import RawResult
from app.sources.stubs import (
    StubBraveSource,
    StubGoogleSource,
    StubOwnCorpusSource,
)


def _raw(url: str, title: str, snippet: str, rank_: int = 1) -> RawResult:
    return RawResult(url=url, title=title, snippet=snippet, source_rank=rank_)


# ---------- source_weight ----------


def test_source_weight_own_corpus():
    assert source_weight(StubOwnCorpusSource()) == 1.0


def test_source_weight_brave():
    assert source_weight(StubBraveSource()) == 0.5


def test_source_weight_google():
    assert source_weight(StubGoogleSource()) == 0.25


def test_source_weight_monotonic_in_priority():
    """Lower priority must always weight ≥ higher priority's weight."""
    own = source_weight(StubOwnCorpusSource())
    brave = source_weight(StubBraveSource())
    google = source_weight(StubGoogleSource())
    assert own >= brave >= google


# ---------- _tokenize ----------


def test_tokenize_lowercases():
    assert _tokenize("Hello World") == ["hello", "world"]


def test_tokenize_splits_on_punctuation():
    assert _tokenize("hello, world!") == ["hello", "world"]


def test_tokenize_empty():
    assert _tokenize("") == []


def test_tokenize_keeps_digits():
    assert _tokenize("python 3.12") == ["python", "3", "12"]


# ---------- rank: edge cases ----------


def test_rank_empty_input():
    assert rank([], "anything") == []


def test_rank_empty_query_preserves_order():
    """Empty query → BM25 has no signal → input order preserved."""
    brave = StubBraveSource()
    pairs = [
        (brave, _raw("a", "Title A", "snippet a")),
        (brave, _raw("b", "Title B", "snippet b")),
    ]
    out = rank(pairs, "")
    assert [p[1].url for p in out] == ["a", "b"]


# ---------- rank: BM25 relevance ----------


def test_rank_promotes_doc_with_more_query_terms():
    """Same source, two candidates — one contains query terms, the other doesn't."""
    brave = StubBraveSource()
    pairs = [
        (brave, _raw("irrelevant", "cake recipe", "how to bake a chocolate cake")),
        (brave, _raw("relevant", "machine learning intro", "basics of ML and neural nets")),
    ]
    out = rank(pairs, "machine learning")
    assert out[0][1].url == "relevant"
    assert out[1][1].url == "irrelevant"


def test_rank_promotes_doc_with_higher_term_frequency():
    """Same source, two candidates both contain query terms — higher tf wins."""
    brave = StubBraveSource()
    pairs = [
        (brave, _raw("one_mention", "fact", "this article mentions python once")),
        (brave, _raw("many_mentions", "python guide", "python python python intro to python")),
    ]
    out = rank(pairs, "python")
    assert out[0][1].url == "many_mentions"
    assert out[1][1].url == "one_mention"


# ---------- rank: source weight + BM25 interaction ----------


def test_source_weight_breaks_content_ties():
    """When two docs have identical content scores, the lower-priority
    source (higher weight) wins."""
    brave = StubBraveSource()
    own = StubOwnCorpusSource()
    pairs = [
        (brave, _raw("b", "machine learning", "tutorial on machine learning")),
        (own, _raw("o", "machine learning", "tutorial on machine learning")),
    ]
    out = rank(pairs, "machine learning")
    assert out[0][1].url == "o"
    assert out[0][0].source_enum.value == "own_corpus"


def test_higher_content_score_can_beat_lower_source_weight():
    """A strong content match from Google can outrank a weak match from
    own_corpus — relevance is the dominant signal at the boundary."""
    google = StubGoogleSource()  # weight 0.25
    own = StubOwnCorpusSource()  # weight 1.0
    pairs = [
        (own, _raw("weak_own", "weather", "today's forecast")),
        (google, _raw("strong_google", "python python python python",
                      "python python python tutorial python")),
    ]
    # With "python" highly mentioned in google's doc and absent in own's,
    # Google's bm25 × 0.25 should still beat Own's bm25 × 1.0 = 0.
    out = rank(pairs, "python")
    assert out[0][1].url == "strong_google"


# ---------- rank: stable on ties ----------


def test_rank_stable_on_full_tie():
    """When every candidate has identical content and same source,
    input order is preserved."""
    brave = StubBraveSource()
    pairs = [
        (brave, _raw(f"u{i}", "title", "snippet content", rank_=i))
        for i in range(5)
    ]
    out = rank(pairs, "title content")
    assert [p[1].source_rank for p in out] == [0, 1, 2, 3, 4]


# ---------- rank: realistic scenario ----------


def test_rank_realistic_three_source_blend():
    """Mixed sources + mixed relevance — own_corpus relevant doc beats
    brave's equally-relevant doc; google's irrelevant doc comes last."""
    own = StubOwnCorpusSource()
    brave = StubBraveSource()
    google = StubGoogleSource()
    pairs = [
        (google, _raw("g_off",  "off-topic", "recipe for pasta")),
        (brave,  _raw("b_rel",  "machine learning intro",
                      "machine learning basics neural networks")),
        (own,    _raw("o_rel",  "machine learning advanced",
                      "machine learning advanced topics neural networks")),
    ]
    out = rank(pairs, "machine learning")
    # Both own and brave are relevant; own's weight (1.0) beats brave's (0.5)
    assert out[0][1].url == "o_rel"
    assert out[1][1].url == "b_rel"
    # Google's irrelevant doc comes last (bm25=0 × any weight = 0)
    assert out[2][1].url == "g_off"


def test_rank_does_not_mutate_input():
    brave = StubBraveSource()
    original = [
        (brave, _raw("a", "title", "snippet machine learning")),
        (brave, _raw("b", "title", "snippet")),
    ]
    snapshot = list(original)
    _ = rank(original, "machine learning")
    assert original == snapshot


# ---------- constant sanity ----------


def test_bm25_constants_match_lucene_defaults():
    """Document the constants — Lucene defaults are k1=1.2 (old) or
    1.5 (modern). 1.5 + 0.75 are what BM25Similarity uses today."""
    assert K1 == 1.5
    assert B == 0.75
