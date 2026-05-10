"""B.7 — /web/extract endpoint + Anthropic OAuth client tests."""

from __future__ import annotations

import json

import pytest

from tests.auth_helpers import sign_test_ept
from tests.test_web_search import RecordingEternitasClient

# ---- AnthropicClient unit ------------------------------------------


def test_anthropic_client_not_configured_without_token():
    from app.anthropic_client import AnthropicClient

    c = AnthropicClient(oauth_token=None)
    assert c.configured is False

    c = AnthropicClient(oauth_token="sk-ant-api03-not-an-oauth-token")
    assert c.configured is False  # only OAuth tokens accepted


def test_anthropic_client_configured_with_oauth_token():
    from app.anthropic_client import AnthropicClient

    c = AnthropicClient(oauth_token="sk-ant-oat01-fake-but-prefixed-correctly")
    assert c.configured is True


@pytest.mark.asyncio
async def test_anthropic_client_messages_sends_correct_shape():
    """Verify Bearer auth, oauth-beta header, and the two-block system array."""
    import httpx

    from app.anthropic_client import CLAUDE_CODE_GATE, AnthropicClient

    captured: dict = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": '{"answer": 42}'}],
        })

    transport = httpx.MockTransport(handler)
    import app.anthropic_client as ac_mod

    real_async_client = ac_mod.httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(transport=transport, timeout=5.0)

    ac_mod.httpx.AsyncClient = patched
    try:
        c = AnthropicClient(oauth_token="sk-ant-oat01-test")
        result = await c.messages(
            system_prompt="extract data",
            user_message="page content here",
        )
        assert result == '{"answer": 42}'

        # Headers
        assert captured["headers"]["authorization"] == "Bearer sk-ant-oat01-test"
        assert captured["headers"]["anthropic-beta"] == "oauth-2025-04-20"
        assert captured["headers"]["anthropic-version"] == "2023-06-01"

        # Body shape — two-block system array, gate first
        sys_blocks = captured["body"]["system"]
        assert isinstance(sys_blocks, list)
        assert len(sys_blocks) == 2
        assert sys_blocks[0] == {"type": "text", "text": CLAUDE_CODE_GATE}
        assert sys_blocks[1]["type"] == "text"
        assert sys_blocks[1]["text"] == "extract data"
    finally:
        ac_mod.httpx.AsyncClient = real_async_client


@pytest.mark.asyncio
async def test_anthropic_client_raises_on_non_200():
    import httpx

    from app.anthropic_client import AnthropicClient

    async def handler(req):
        return httpx.Response(429, text='{"error": "rate"}')

    transport = httpx.MockTransport(handler)
    import app.anthropic_client as ac_mod

    real = ac_mod.httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real(transport=transport, timeout=5.0)

    ac_mod.httpx.AsyncClient = patched
    try:
        c = AnthropicClient(oauth_token="sk-ant-oat01-test")
        with pytest.raises(RuntimeError) as exc_info:
            await c.messages(system_prompt="x", user_message="y")
        assert "429" in str(exc_info.value)
    finally:
        ac_mod.httpx.AsyncClient = real


# ---- extract_structured_data unit ----------------------------------


class _StubAnthropic:
    """Drop-in for AnthropicClient that returns canned text without HTTP."""

    def __init__(self, response_text: str, raise_exc: Exception | None = None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.last_call: dict | None = None

    @property
    def configured(self) -> bool:
        return True

    async def messages(self, *, system_prompt, user_message, max_tokens=4000):
        self.last_call = {
            "system_prompt": system_prompt,
            "user_message": user_message,
            "max_tokens": max_tokens,
        }
        if self.raise_exc:
            raise self.raise_exc
        return self.response_text


@pytest.mark.asyncio
async def test_extract_returns_parsed_json():
    from app.web.extract import extract_structured_data

    stub = _StubAnthropic(response_text='{"title": "Hello", "price": 19.99}')
    result = await extract_structured_data(
        page_content="<html>...</html>",
        schema={"type": "object", "properties": {"title": {"type": "string"}}},
        instruction=None,
        anthropic_client=stub,
    )
    assert result == {"title": "Hello", "price": 19.99}


@pytest.mark.asyncio
async def test_extract_strips_markdown_fence():
    from app.web.extract import extract_structured_data

    stub = _StubAnthropic(response_text='```json\n{"x": 1}\n```')
    result = await extract_structured_data("body", {}, None, stub)
    assert result == {"x": 1}


@pytest.mark.asyncio
async def test_extract_handles_bare_fence_no_lang():
    from app.web.extract import extract_structured_data

    stub = _StubAnthropic(response_text='```\n{"x": 1}\n```')
    result = await extract_structured_data("body", {}, None, stub)
    assert result == {"x": 1}


@pytest.mark.asyncio
async def test_extract_includes_instruction_in_user_message():
    from app.web.extract import extract_structured_data

    stub = _StubAnthropic(response_text='{}')
    await extract_structured_data(
        page_content="page",
        schema={"type": "object"},
        instruction="Focus on prices in USD",
        anthropic_client=stub,
    )
    assert "Focus on prices in USD" in stub.last_call["user_message"]


@pytest.mark.asyncio
async def test_extract_raises_on_non_json():
    from app.web.extract import extract_structured_data

    stub = _StubAnthropic(response_text="I cannot extract that.")
    with pytest.raises(RuntimeError) as exc_info:
        await extract_structured_data("body", {}, None, stub)
    assert "non-JSON" in str(exc_info.value)


# ---- /web/extract endpoint ----------------------------------------


def _patch_fetch_with_html(monkeypatch, html: str):
    from app.web.fetch import FetchResponse

    async def fake_fetch(url, *, max_chars, offset, **kwargs):
        # Mirror real fetch's HTML-strip
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return FetchResponse(
            final_url=url,
            status_code=200,
            content_type="text/html",
            content=text,
            total_chars=len(text),
            offset=0,
            max_chars=max_chars,
            truncated=False,
        )

    monkeypatch.setattr("app.web.router.fetch_url", fake_fetch)


@pytest.mark.asyncio
async def test_extract_requires_authorization(gated_client):
    resp = await gated_client.post(
        "/web/extract",
        json={"url": "https://example.com/", "schema": {}},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_extract_503_when_anthropic_unconfigured(gated_client, ept_keypair):
    """Service degrades cleanly when Anthropic isn't wired."""
    from app.main import app

    saved = getattr(app.state, "anthropic_client", None)
    app.state.anthropic_client = None
    try:
        token = sign_test_ept(ept_keypair)
        resp = await gated_client.post(
            "/web/extract",
            headers={"Authorization": f"Bearer {token}"},
            json={"url": "https://example.com/", "schema": {}},
        )
        assert resp.status_code == 503
        assert "Anthropic" in resp.json()["detail"]
    finally:
        app.state.anthropic_client = saved


@pytest.mark.asyncio
async def test_extract_happy_path_with_event_post(gated_client, ept_keypair, monkeypatch):
    """Full pipeline: fetch → Claude → JSON → integrity event."""
    from app.main import app

    app.state.anthropic_client = _StubAnthropic(
        response_text='{"title": "Test Page", "price": 19.99}'
    )
    app.state.eternitas_client = RecordingEternitasClient()

    _patch_fetch_with_html(monkeypatch, "<h1>Test Page</h1><span>$19.99</span>")

    token = sign_test_ept(ept_keypair, passport="ET26-EXTR-AAAA")
    resp = await gated_client.post(
        "/web/extract",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "url": "https://example.com/page",
            "schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "price": {"type": "number"},
                },
            },
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["url"] == "https://example.com/page"
    assert data["extracted"] == {"title": "Test Page", "price": 19.99}
    assert data["integrity_event_posted"] is True

    # Eternitas got the right event
    calls = app.state.eternitas_client.calls
    assert len(calls) == 1
    assert calls[0]["event_type"] == "web.extract.completed"
    assert calls[0]["dimension"] == "reliability"
    assert calls[0]["delta_hint"] == 1


@pytest.mark.asyncio
async def test_extract_502_on_anthropic_error(gated_client, ept_keypair, monkeypatch):
    from app.main import app

    app.state.anthropic_client = _StubAnthropic(
        response_text="",
        raise_exc=RuntimeError("Anthropic API 429: rate limit"),
    )
    _patch_fetch_with_html(monkeypatch, "<p>page</p>")

    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/extract",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "https://example.com/", "schema": {}},
    )
    assert resp.status_code == 502
    assert "429" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_extract_400_on_unsafe_url(gated_client, ept_keypair, monkeypatch):
    from app.main import app
    from app.web.fetch import UnsafeURLError

    app.state.anthropic_client = _StubAnthropic(response_text='{}')

    async def boom(url, **kwargs):
        raise UnsafeURLError("blocked")

    monkeypatch.setattr("app.web.router.fetch_url", boom)

    token = sign_test_ept(ept_keypair)
    resp = await gated_client.post(
        "/web/extract",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "http://10.0.0.1/", "schema": {}},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_extract_charges_correct_capability_cost(gated_client, ept_keypair, monkeypatch):
    """web.extract is 20_000 microcents = $0.02."""
    from app.eii.cost_cap import _key
    from app.main import app

    app.state.anthropic_client = _StubAnthropic(response_text='{"x":1}')
    app.state.eternitas_client = RecordingEternitasClient()
    _patch_fetch_with_html(monkeypatch, "<p>x</p>")

    token = sign_test_ept(ept_keypair, passport="ET26-COST-EXTR")
    resp = await gated_client.post(
        "/web/extract",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "https://example.com/", "schema": {}},
    )
    assert resp.status_code == 200
    assert app.state.redis._strings[_key("ET26-COST-EXTR")] == 20_000
    assert resp.headers["X-Cost-Capability"] == "web.extract"
