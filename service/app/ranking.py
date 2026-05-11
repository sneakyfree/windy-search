"""BM25 + source-priority weighted ranker (M1.5).

Replaces the M1.2 priority-sorted concat with a content-aware score
that combines:

  1. **BM25(title + snippet, query)** — relevance signal using the
     classical Okapi BM25 formula with Lucene-default constants
     (`k1=1.5, b=0.75`). The "corpus" against which IDF is computed
     is the per-query candidate set (small N, in-memory), not a
     global index — that lands in M3+ when the own-corpus reader
     ships.

  2. **Source priority weight** — multiplicative; weight =
     `1.0 / (1.0 + priority × 0.1)`.

       own_corpus(0)  → 1.00
       brave(10)      → 0.50
       google(30)     → 0.25

     Matches master plan §4 P1 (Brave primary, Google last-resort)
     and §4 P2 (own corpus preferred when available).

  3. **Recency** — planned for M3 when `Provenance.indexed_at` and
     `fetched_at` actually carry useful timestamps. For M1 stubs all
     timestamps default to "now"; recency weight is a no-op.

Combined score = `bm25_score × source_priority_weight`. Ties (e.g.,
empty query, identical docs) preserve input order via stable sort
with original index as the tiebreaker.

Per master plan §6 M1.5 + ADR-014.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence

from app.sources.base import RawResult, Source

# Lucene defaults — well-studied across English text corpora.
K1 = 1.5
B = 0.75


def rank(
    candidates: Sequence[tuple[Source, RawResult]],
    query: str,
) -> list[tuple[Source, RawResult]]:
    """Re-order candidates by combined relevance score (descending).

    Empty input ⇒ empty output. Empty query ⇒ preserve input order
    (BM25 has no signal without query terms). Stable tiebreaker: when
    two candidates score equally, the earlier-in-input one wins.
    """
    if not candidates:
        return []

    query_terms = _tokenize(query)
    if not query_terms:
        return list(candidates)

    documents = [_tokenize(f"{r.title} {r.snippet}") for _, r in candidates]
    avgdl = sum(len(d) for d in documents) / len(documents) if documents else 0.0
    idfs = {term: _idf(term, documents) for term in set(query_terms)}

    scored: list[tuple[float, int, Source, RawResult]] = []
    for i, ((source, raw), doc) in enumerate(zip(candidates, documents)):
        bm25 = _bm25(query_terms, doc, idfs, avgdl)
        weight = source_weight(source)
        scored.append((bm25 * weight, i, source, raw))

    # Sort by descending score; ties break by original input index (stable).
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(s, r) for _, _, s, r in scored]


def source_weight(source: Source) -> float:
    """Multiplicative weight applied to BM25 score. Lower priority ⇒
    higher weight ⇒ result rises in the merged response."""
    return 1.0 / (1.0 + source.priority * 0.1)


def _tokenize(text: str) -> list[str]:
    """Lower-case + split on non-alphanumeric. Returns the token sequence
    (not a set) so term frequency stays meaningful."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _idf(term: str, documents: Sequence[list[str]]) -> float:
    """Probabilistic IDF: `log((N - n + 0.5) / (n + 0.5) + 1)`.

    The `+1` guards against negative IDF when a term appears in more
    than half the documents — matches Lucene's `BM25Similarity`.
    """
    n = sum(1 for d in documents if term in d)
    n_total = len(documents)
    return math.log((n_total - n + 0.5) / (n + 0.5) + 1)


def _bm25(
    query_terms: Sequence[str],
    doc: Sequence[str],
    idfs: dict[str, float],
    avgdl: float,
) -> float:
    """Okapi BM25 score for `doc` against `query_terms` given the
    corpus-level `idfs` table and `avgdl` average doc length."""
    if not doc or avgdl == 0:
        return 0.0
    score = 0.0
    dl = len(doc)
    for term in query_terms:
        f = doc.count(term)
        if f == 0:
            continue
        idf = idfs.get(term, 0.0)
        numerator = f * (K1 + 1)
        denominator = f + K1 * (1 - B + B * dl / avgdl)
        score += idf * numerator / denominator
    return score
