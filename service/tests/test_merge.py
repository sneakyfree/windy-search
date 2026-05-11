"""Unit tests for app/merge.py (M1.7).

Covers:
  * `apply_per_source_cap` — drops trailing entries per source once the
    cap is hit; preserves input order among survivors; floor of 2
    ensures small `max_results` still passes results through.
"""
from __future__ import annotations

from app.merge import DEFAULT_MAX_FRACTION, apply_per_source_cap
from app.sources.base import RawResult
from app.sources.stubs import (
    StubBraveSource,
    StubGoogleSource,
    StubOwnCorpusSource,
)


def _raw(i: int) -> RawResult:
    return RawResult(url=f"https://a.com/{i}", title="t", snippet="s", source_rank=i)


def test_empty_input_returns_empty():
    assert apply_per_source_cap([], max_results=10) == []


def test_no_cap_when_under_threshold():
    """With max_results=10 + default 0.7, per-source cap is 7. Brave
    contributes 3 — well under the cap, so all 3 survive."""
    brave = StubBraveSource()
    pairs = [(brave, _raw(i)) for i in range(3)]
    out = apply_per_source_cap(pairs, max_results=10)
    assert len(out) == 3


def test_cap_drops_trailing_entries():
    """Brave contributes 12 but cap is 7 → keep first 7, drop last 5."""
    brave = StubBraveSource()
    pairs = [(brave, _raw(i)) for i in range(12)]
    out = apply_per_source_cap(pairs, max_results=10)
    assert len(out) == 7
    # First 7 source_ranks preserved
    assert [p[1].source_rank for p in out] == list(range(7))


def test_each_source_gets_its_own_cap():
    """With max_results=10, cap per source = 7. Two sources each with 10 results
    → 7 from each = 14 total."""
    brave = StubBraveSource()
    google = StubGoogleSource()
    pairs = (
        [(brave, _raw(i)) for i in range(10)]
        + [(google, _raw(100 + i)) for i in range(10)]
    )
    out = apply_per_source_cap(pairs, max_results=10)
    assert len(out) == 14
    brave_count = sum(1 for s, _ in out if s.name == "stub_brave")
    google_count = sum(1 for s, _ in out if s.name == "stub_google")
    assert brave_count == 7
    assert google_count == 7


def test_input_order_preserved():
    """Interleaved sources keep their input order after capping."""
    brave = StubBraveSource()
    google = StubGoogleSource()
    pairs = [
        (brave, _raw(1)),
        (google, _raw(2)),
        (brave, _raw(3)),
        (google, _raw(4)),
        (brave, _raw(5)),
    ]
    out = apply_per_source_cap(pairs, max_results=10)
    assert [p[1].source_rank for p in out] == [1, 2, 3, 4, 5]


def test_floor_of_two_for_small_max_results():
    """max_results=1 → cap = max(2, ceil(1 * 0.7)) = 2. Sources can still
    contribute up to 2 — the router's final slice handles the actual
    response limit."""
    brave = StubBraveSource()
    pairs = [(brave, _raw(i)) for i in range(5)]
    out = apply_per_source_cap(pairs, max_results=1)
    assert len(out) == 2


def test_max_fraction_one_disables_capping():
    """max_fraction=1.0 means a single source can fill the whole response."""
    brave = StubBraveSource()
    pairs = [(brave, _raw(i)) for i in range(50)]
    out = apply_per_source_cap(pairs, max_results=10, max_fraction=1.0)
    assert len(out) == 10  # ceil(10 * 1.0) = 10


def test_custom_max_fraction():
    """max_fraction=0.5 + max_results=10 → cap = 5."""
    brave = StubBraveSource()
    pairs = [(brave, _raw(i)) for i in range(10)]
    out = apply_per_source_cap(pairs, max_results=10, max_fraction=0.5)
    assert len(out) == 5


def test_default_max_fraction_value():
    """Document the default — 0.7 chosen so cap binds only on pathological cases."""
    assert DEFAULT_MAX_FRACTION == 0.7


def test_three_sources_full_response_each_under_cap():
    """Realistic scenario: 3 sources × 5 results = 15 raw, cap=7 each →
    all 15 survive."""
    own = StubOwnCorpusSource()
    brave = StubBraveSource()
    google = StubGoogleSource()
    pairs = (
        [(own, _raw(i)) for i in range(5)]
        + [(brave, _raw(100 + i)) for i in range(5)]
        + [(google, _raw(200 + i)) for i in range(5)]
    )
    out = apply_per_source_cap(pairs, max_results=10)
    assert len(out) == 15
