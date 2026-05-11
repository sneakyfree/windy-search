"""Tests for /version (deployment-identity endpoint).

See ~/kit-army-config/docs/marathon-foundations-program-2026-05-11.md §MF1.
"""
import pytest


@pytest.mark.asyncio
async def test_version_returns_200(client):
    resp = await client.get("/version")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_version_shape(client):
    resp = await client.get("/version")
    data = resp.json()
    for key in (
        "service",
        "version",
        "commit_sha",
        "commit_sha_short",
        "build_timestamp",
        "started_at",
        "environment",
    ):
        assert key in data, f"missing field: {key}"
    assert data["service"] == "windy-search-api"
    assert isinstance(data["version"], str) and data["version"]
    assert isinstance(data["started_at"], str) and data["started_at"]


@pytest.mark.asyncio
async def test_version_reflects_env_vars(client, monkeypatch):
    monkeypatch.setenv("COMMIT_SHA", "abc1234567890abcdef1234567890abcdef12345")
    monkeypatch.setenv("BUILD_TIMESTAMP", "2026-05-11T12:00:00Z")
    monkeypatch.setenv("ENVIRONMENT", "production")

    resp = await client.get("/version")
    data = resp.json()
    assert data["commit_sha"] == "abc1234567890abcdef1234567890abcdef12345"
    assert data["commit_sha_short"] == "abc1234"
    assert data["build_timestamp"] == "2026-05-11T12:00:00Z"
    assert data["environment"] == "production"


@pytest.mark.asyncio
async def test_version_unset_env_returns_nulls(client, monkeypatch):
    monkeypatch.delenv("COMMIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_TIMESTAMP", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    resp = await client.get("/version")
    data = resp.json()
    assert data["commit_sha"] is None
    assert data["commit_sha_short"] is None
    assert data["build_timestamp"] is None
    assert data["environment"] == "unknown"


@pytest.mark.asyncio
async def test_version_in_openapi_spec(client):
    resp = await client.get("/openapi.json")
    spec = resp.json()
    assert "/version" in spec["paths"]
    get_op = spec["paths"]["/version"]["get"]
    assert "health" in get_op.get("tags", [])
