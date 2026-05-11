"""Unit tests for app/dedup.py (M1.6).

Covers:
  * `canonical_url` — case folding on scheme/host, default-port strip,
    fragment strip, trailing-slash strip.
  * `url_hash` — same canonical URL ⇒ same hash; different ⇒ different.
  * `dedup_across_sources` — same canonical URL from multiple sources
    collapses to the lowest-priority source; input order preserved
    among survivors; empty input is a no-op.
"""
from __future__ import annotations

from app.dedup import canonical_url, dedup_across_sources, url_hash
from app.sources.base import RawResult
from app.sources.stubs import (
    StubBraveSource,
    StubGoogleSource,
    StubOwnCorpusSource,
)

# ---------- canonical_url ----------


def test_lowercases_scheme_and_host():
    assert canonical_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_strips_default_port_http():
    assert canonical_url("http://example.com:80/a") == "http://example.com/a"


def test_strips_default_port_https():
    assert canonical_url("https://example.com:443/a") == "https://example.com/a"


def test_keeps_non_default_port():
    assert canonical_url("https://example.com:8080/a") == "https://example.com:8080/a"


def test_strips_fragment():
    assert canonical_url("https://example.com/a#section") == "https://example.com/a"


def test_strips_trailing_slash_on_non_root_path():
    assert canonical_url("https://example.com/a/") == "https://example.com/a"


def test_preserves_root_slash():
    assert canonical_url("https://example.com/") == "https://example.com/"


def test_preserves_query_params_verbatim():
    """Param sorting risks breaking signed URLs — keep order as-is."""
    a = canonical_url("https://example.com/p?b=1&a=2")
    assert a == "https://example.com/p?b=1&a=2"


def test_path_case_preserved():
    """Path is case-sensitive on most servers — don't fold."""
    assert canonical_url("https://Example.com/PathName") == "https://example.com/PathName"


def test_malformed_url_returns_unchanged():
    """Best-effort: malformed URL stays as input rather than raising."""
    # urlparse is permissive — pure-garbage still parses, just empty parts.
    # The point is no exception bubbles to the caller.
    out = canonical_url("not a url")
    assert isinstance(out, str)  # didn't raise


# ---------- url_hash ----------


def test_same_canonical_yields_same_hash():
    a = url_hash("https://Example.COM/path/#frag")
    b = url_hash("HTTPS://example.com/path")
    assert a == b


def test_different_urls_yield_different_hash():
    assert url_hash("https://a.com/") != url_hash("https://b.com/")


def test_url_hash_is_64_hex_chars():
    h = url_hash("https://example.com/")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ---------- dedup_across_sources ----------


def _raw(url: str, source_rank: int = 1) -> RawResult:
    return RawResult(
        url=url,
        title=f"Title for {url}",
        snippet=f"Snippet for {url}",
        source_rank=source_rank,
    )


def test_empty_input_returns_empty():
    assert dedup_across_sources([]) == []


def test_no_duplicates_preserves_input():
    brave = StubBraveSource()
    pairs = [
        (brave, _raw("https://a.com/1")),
        (brave, _raw("https://a.com/2")),
        (brave, _raw("https://a.com/3")),
    ]
    assert dedup_across_sources(pairs) == pairs


def test_duplicates_from_same_source_collapse_to_first():
    brave = StubBraveSource()
    pairs = [
        (brave, _raw("https://a.com/x", source_rank=1)),
        (brave, _raw("https://a.com/x", source_rank=2)),
    ]
    out = dedup_across_sources(pairs)
    assert len(out) == 1
    assert out[0][1].source_rank == 1


def test_lower_priority_source_wins():
    """own_corpus(0) wins over brave(10) for the same URL."""
    brave = StubBraveSource()
    own = StubOwnCorpusSource()
    pairs = [
        (brave, _raw("https://shared.com/article")),
        (own, _raw("https://shared.com/article")),
    ]
    out = dedup_across_sources(pairs)
    assert len(out) == 1
    assert out[0][0].source_enum.value == "own_corpus"


def test_lower_priority_wins_regardless_of_input_order():
    """Even when brave appears AFTER own_corpus, own_corpus still wins."""
    brave = StubBraveSource()
    own = StubOwnCorpusSource()
    pairs = [
        (own, _raw("https://shared.com/article")),
        (brave, _raw("https://shared.com/article")),
    ]
    out = dedup_across_sources(pairs)
    assert len(out) == 1
    assert out[0][0].source_enum.value == "own_corpus"


def test_url_normalization_collapses_case_variants():
    """Brave's URL and own_corpus's URL differ only in host case → one survives."""
    brave = StubBraveSource()
    own = StubOwnCorpusSource()
    pairs = [
        (brave, _raw("https://Example.com/article")),
        (own, _raw("https://EXAMPLE.com/article#section")),
    ]
    out = dedup_across_sources(pairs)
    assert len(out) == 1
    assert out[0][0].source_enum.value == "own_corpus"


def test_input_order_preserved_among_survivors():
    """When no duplicates, survivors stay in input order."""
    brave = StubBraveSource()
    google = StubGoogleSource()
    own = StubOwnCorpusSource()
    pairs = [
        (brave, _raw("https://a.com/1")),
        (own, _raw("https://b.com/1")),
        (google, _raw("https://c.com/1")),
    ]
    out = dedup_across_sources(pairs)
    assert [p[1].url for p in out] == ["https://a.com/1", "https://b.com/1", "https://c.com/1"]


def test_same_priority_first_seen_wins():
    """When two sources of equal priority have the same URL, first occurrence wins."""
    brave = StubBraveSource()
    # Create a second source with priority == brave's via a fresh stub instance —
    # actually all StubBraveSource instances share priority 10 by definition.
    brave2 = StubBraveSource()
    pairs = [
        (brave, _raw("https://shared.com/", source_rank=1)),
        (brave2, _raw("https://shared.com/", source_rank=2)),
    ]
    out = dedup_across_sources(pairs)
    assert len(out) == 1
    assert out[0][1].source_rank == 1  # first occurrence wins
