"""Windy Mind broker routing per ADR-022 §5 (BYOM moat).

Mind is the intelligence kernel; every Windy LLM call should broker
through it for cost transparency, observability, fallback, and BYOM
model choice. This module is the sed-clone counterpart to
`windy-agent/src/windyfly/agent/models.py::_try_mind_broker` adapted
to windy-search's async style.

OPT-IN via env vars — no behavior change for callers that lack EPT:
  ETERNITAS_PASSPORT_TOKEN (or ETERNITAS_PASSPORT) — the agent's EPT
  MIND_API_URL — defaults to https://api.windymind.ai

Returns the assistant text content on success, or None on any
opt-out / failure — caller falls through to its direct-provider path.
Zero regression risk: when no EPT is configured (pre-hatch boot,
test rigs, third-party callers), this function is a no-op.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _ept_from_env() -> str | None:
    return os.environ.get("ETERNITAS_PASSPORT_TOKEN") or os.environ.get("ETERNITAS_PASSPORT")


def _mind_url_from_env() -> str:
    return os.environ.get("MIND_API_URL", "https://api.windymind.ai").rstrip("/")


def _extract_text_from_response(payload: dict[str, Any]) -> str | None:
    """Mind's /v1/chat is OpenAI-compatible — text lives at
    choices[0].message.content. Defensive: returns None if the shape
    is unexpected so the caller falls through cleanly."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return None
    return content


async def try_mind_broker(
    *,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4000,
    model: str | None = None,
    timeout_seconds: float = 30.0,
) -> str | None:
    """Async POST to Mind /v1/chat. Returns the assistant text on
    success, None on opt-out (no EPT) or any failure.

    Matches the windy-search async style — caller awaits in the same
    flow as `AnthropicClient.messages`."""
    ept = _ept_from_env()
    if not ept:
        return None

    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
    }
    if model:
        body["model"] = model

    mind_url = _mind_url_from_env()

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                f"{mind_url}/v1/chat",
                headers={
                    "Authorization": f"Bearer {ept}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except Exception as e:
        logger.info("Mind broker call failed (%s); falling through", e)
        return None

    if resp.status_code != 200:
        logger.info(
            "Mind broker returned %s; falling through to direct chain",
            resp.status_code,
        )
        return None

    try:
        payload = resp.json()
    except Exception as e:
        logger.info("Mind broker non-JSON response (%s); falling through", e)
        return None

    text = _extract_text_from_response(payload)
    if text is None:
        logger.info("Mind broker returned unexpected shape; falling through")
    return text
