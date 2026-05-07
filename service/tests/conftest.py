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
