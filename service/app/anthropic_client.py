"""Anthropic API client wired for OAuth tokens (sk-ant-oat01-*).

Per `feedback_anthropic_oauth_gate.md`, OAuth tokens against
`api.anthropic.com/v1/messages` require:

  1. Bearer auth (NOT x-api-key)
  2. `anthropic-beta: oauth-2025-04-20`
  3. The system field MUST be a content-block array whose FIRST block
     is exactly: "You are Claude Code, Anthropic's official CLI for Claude."
     Block 1+ carry our actual system prompt.

Any other shape returns a generic 429 "Error" with no detail — easy to
mistake for a real rate limit.

ToS scope: this only authorizes using Grant's own subscription quota
the way Claude Code does. Multi-user fan-out at scale = ToS violation.
For multi-user, switch the call site to per-user `sk-ant-api03-*` keys
or AWS Bedrock.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Required first block per OAuth gate. Exact string — no trailing newline.
CLAUDE_CODE_GATE = "You are Claude Code, Anthropic's official CLI for Claude."

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"


class AnthropicClient:
    def __init__(
        self,
        oauth_token: Optional[str],
        model: str = "claude-sonnet-4-6",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.oauth_token = oauth_token
        self.model = model
        self.timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.oauth_token) and self.oauth_token.startswith("sk-ant-oat01-")

    async def messages(
        self,
        *,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 4000,
    ) -> str:
        """Single-turn /v1/messages call. Returns the assistant text content.

        Raises RuntimeError on non-200 with the upstream status + body
        prefix — caller maps to HTTPException as appropriate.
        """
        if not self.configured:
            raise RuntimeError("Anthropic client not configured (missing or non-OAuth token)")

        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [
                {"type": "text", "text": CLAUDE_CODE_GATE},
                {"type": "text", "text": system_prompt},
            ],
            "messages": [{"role": "user", "content": user_message}],
        }
        headers = {
            "Authorization": f"Bearer {self.oauth_token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ANTHROPIC_OAUTH_BETA,
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(ANTHROPIC_API_URL, headers=headers, json=body)

        if resp.status_code != 200:
            # Trim the body to keep logs sane but include enough to debug
            # the OAuth-gate footguns (429 with no body, 401 missing beta).
            raise RuntimeError(
                f"Anthropic API {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        # content is an array of blocks; we expect a single text block for
        # this single-turn use. Defensive handling: concatenate any text
        # blocks if the model returned more than one.
        parts: list[str] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
