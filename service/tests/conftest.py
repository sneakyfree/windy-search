"""Pytest fixtures — minimal scaffold for B.1; B.2 adds auth helpers."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.auth_helpers import StubJWKSCache, generate_ept_keypair


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def ept_keypair():
    """A fresh ES256 keypair + matching JWKS for each test."""
    return generate_ept_keypair()


@pytest_asyncio.fixture
async def auth_client(ept_keypair):
    """Test client with a stub JWKS cache pre-injected — EPT verification
    works against test-issued tokens without going to network."""
    app.state.jwks_cache = StubJWKSCache(ept_keypair["jwks"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.state.jwks_cache = None


# ---- B.3 fixtures ---------------------------------------------------


class StubScoreCache:
    """Score cache that returns hardcoded values without hitting eternitas."""

    def __init__(self, default_score: int = 500) -> None:
        self.default_score = default_score
        self.scores: dict[str, int] = {}

    async def get(self, passport: str) -> int:
        return self.scores.get(passport, self.default_score)

    def invalidate(self, passport: str | None = None) -> None:
        if passport is None:
            self.scores.clear()
        else:
            self.scores.pop(passport, None)


class FakeRedisB3:
    """Minimal redis stub supporting the sliding-window rate-limit ops.

    zremrangebyscore + zadd + zcard + expire — ZADD with a unique member
    per call so repeated calls inside the same millisecond still count
    independently."""

    def __init__(self) -> None:
        self._zsets: dict[str, dict[str, float]] = {}

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    def pipeline(self) -> "FakeRedisPipelineB3":
        return FakeRedisPipelineB3(self)


class FakeRedisPipelineB3:
    def __init__(self, parent: FakeRedisB3) -> None:
        self.parent = parent
        self._results: list = []

    def zremrangebyscore(self, key, mn, mx):
        zset = self.parent._zsets.setdefault(key, {})
        removed = [m for m, score in zset.items() if mn <= score <= mx]
        for m in removed:
            zset.pop(m, None)
        self._results.append(len(removed))
        return self

    def zadd(self, key, mapping):
        zset = self.parent._zsets.setdefault(key, {})
        zset.update(mapping)
        self._results.append(len(mapping))
        return self

    def zcard(self, key):
        zset = self.parent._zsets.get(key, {})
        self._results.append(len(zset))
        return self

    def expire(self, key, seconds):
        self._results.append(True)
        return self

    async def execute(self):
        return self._results


@pytest_asyncio.fixture
async def gated_client(ept_keypair):
    """Test client wired with stubs for JWKS, score cache, and Redis —
    exercises the full B.2 + B.3 dependency chain end-to-end."""
    app.state.jwks_cache = StubJWKSCache(ept_keypair["jwks"])
    app.state.score_cache = StubScoreCache(default_score=500)
    app.state.redis = FakeRedisB3()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.state.jwks_cache = None
    app.state.score_cache = None
    app.state.redis = None
