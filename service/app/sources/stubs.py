"""Deterministic stub sources for M1 router development + tests.

Each stub returns a fixed result set keyed on `(source_name, query)`. M2
replaces these with real bridge adapters; the abstract base in `base.py`
is unchanged.

The stubs let the router + normalization + (later) ranker/dedup/merge be
tested without paying real bridge costs or depending on network access.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from app.sources.base import RawResult, Source
from app.types import BridgeSource


def _deterministic_hash(parts: tuple[str, ...]) -> str:
    """Stable 8-char hex digest used to make fake URLs unique per query."""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


class StubBraveSource(Source):
    """3 mock results per query. Priority 10 = primary bridge per
    master plan §4 P1 (Brave is the primary external bridge)."""

    @property
    def name(self) -> str:
        return "stub_brave"

    @property
    def source_enum(self) -> BridgeSource:
        return BridgeSource.BRAVE

    @property
    def priority(self) -> int:
        return 10

    @property
    def cost_per_query(self) -> Decimal:
        return Decimal("0.005")

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        h = _deterministic_hash(("brave", query))
        return [
            RawResult(
                url=f"https://brave.example/{h}/{i}",
                title=f"Brave result {i} for {query!r}",
                snippet=f"Stub Brave snippet #{i} for the query {query!r}.",
                source_rank=i,
            )
            for i in range(1, 4)
        ]


class StubGoogleSource(Source):
    """2 mock results per query. Priority 30 = last-resort fallback per
    master plan §4 P1 (Google as last-resort, not primary)."""

    @property
    def name(self) -> str:
        return "stub_google"

    @property
    def source_enum(self) -> BridgeSource:
        return BridgeSource.GOOGLE

    @property
    def priority(self) -> int:
        return 30

    @property
    def cost_per_query(self) -> Decimal:
        return Decimal("0.005")

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        h = _deterministic_hash(("google", query))
        return [
            RawResult(
                url=f"https://google.example/{h}/{i}",
                title=f"Google result {i} for {query!r}",
                snippet=f"Stub Google snippet #{i} for the query {query!r}.",
                source_rank=i,
            )
            for i in range(1, 3)
        ]


class StubOwnCorpusSource(Source):
    """2 mock results per query. Priority 0 = always first per master
    plan §4 P2 (own corpus weans bridges over time; preferred when
    available)."""

    @property
    def name(self) -> str:
        return "stub_own_corpus"

    @property
    def source_enum(self) -> BridgeSource:
        return BridgeSource.OWN_CORPUS

    @property
    def priority(self) -> int:
        return 0

    @property
    def cost_per_query(self) -> Decimal:
        return Decimal("0")

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        h = _deterministic_hash(("own_corpus", query))
        return [
            RawResult(
                url=f"https://owncorpus.example/{h}/{i}",
                title=f"Own-corpus result {i} for {query!r}",
                snippet=f"Stub own-corpus snippet #{i} for the query {query!r}.",
                source_rank=i,
            )
            for i in range(1, 3)
        ]


class _UnconfiguredStubSource(Source):
    """Test fixture — returns is_configured()=False. The router must skip it.

    Underscore prefix because this is for internal test use only, not a
    real source. Lives in stubs.py rather than tests/ so the unit tests
    don't need to define a one-off subclass of Source.
    """

    @property
    def name(self) -> str:
        return "_unconfigured_stub"

    @property
    def source_enum(self) -> BridgeSource:
        return BridgeSource.STACKEXCHANGE  # arbitrary; unused

    @property
    def priority(self) -> int:
        return 100

    @property
    def cost_per_query(self) -> Decimal:
        return Decimal("0")

    def is_configured(self) -> bool:
        return False

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        # If the router ever DOES call us, raise — the test should fail
        # loudly because that's a router-bug regression.
        raise AssertionError(
            "_UnconfiguredStubSource.search must not be called: "
            "router should skip unconfigured sources"
        )


class _BrokenStubSource(Source):
    """Test fixture — raises on every search. The router must NOT bubble
    the exception; the other sources still answer."""

    @property
    def name(self) -> str:
        return "_broken_stub"

    @property
    def source_enum(self) -> BridgeSource:
        return BridgeSource.YOUTUBE  # arbitrary; unused

    @property
    def priority(self) -> int:
        return 50

    @property
    def cost_per_query(self) -> Decimal:
        return Decimal("0")

    async def search(self, query: str, **opts: Any) -> list[RawResult]:
        raise RuntimeError("simulated bridge failure")
