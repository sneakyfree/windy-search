"""Windy Hand render backend tests — the Phase-2 slot swap."""

from __future__ import annotations

import httpx
import pytest

from app.web.windyhand import WindyHandRenderer


def test_unconfigured_when_url_unset():
    assert WindyHandRenderer(None).is_configured() is False
    assert WindyHandRenderer("").is_configured() is False


def test_configured_strips_trailing_slash():
    r = WindyHandRenderer("http://127.0.0.1:8560/")
    assert r.is_configured() is True
    assert r._base_url == "http://127.0.0.1:8560"


async def test_render_requires_ept():
    r = WindyHandRenderer("http://127.0.0.1:8560")
    with pytest.raises(RuntimeError, match="EPT"):
        await r.render("https://example.com/", ept=None)


async def test_render_maps_response(monkeypatch):
    captured = {}

    async def fake_post(self, url, *, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return httpx.Response(
            200,
            json={
                "final_url": "https://example.com/post-redirect",
                "status_code": 200,
                "text": "Hydrated body text",
                "rendered_via": "windy-hand",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    r = WindyHandRenderer("http://127.0.0.1:8560")
    result = await r.render("https://example.com/", ept="test-ept")

    assert captured["url"] == "http://127.0.0.1:8560/render"
    assert captured["headers"]["Authorization"] == "Bearer test-ept"
    assert captured["json"]["url"] == "https://example.com/"
    assert result.final_url == "https://example.com/post-redirect"
    assert result.content == "Hydrated body text"
    assert result.total_chars == len("Hydrated body text")


async def test_render_non_200_raises(monkeypatch):
    async def fake_post(self, url, *, headers=None, json=None):
        return httpx.Response(403, json={"detail": "robots.txt disallows WindyHandBot"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    r = WindyHandRenderer("http://127.0.0.1:8560")
    with pytest.raises(RuntimeError, match="403"):
        await r.render("https://example.com/", ept="test-ept")


async def test_render_validates_url_before_posting():
    r = WindyHandRenderer("http://127.0.0.1:8560")
    from app.web.fetch import UnsafeURLError

    with pytest.raises(UnsafeURLError):
        await r.render("http://169.254.169.254/latest/meta-data/", ept="t")
