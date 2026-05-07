"""Structured-data extraction from web pages (Phase B.7).

Composes Phase B.5 (fetch with SSRF protection + cache) with the
Anthropic client to extract JSON-Schema-shaped structured data from
arbitrary URLs.

v1: text-based — we send Claude the HTML-stripped page content. Visual
extraction (Claude vision over a Browserbase screenshot) lands as a
B.7b codon once Phase B.6 (browse) is wired.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Bound the user message size — Claude has long context but extraction
# accuracy degrades on very long inputs and tokens cost money.
MAX_CONTENT_CHARS = 30_000

EXTRACT_SYSTEM_PROMPT = (
    "You are a structured-data extraction tool. Given web page content "
    "and a JSON Schema, return a JSON object that matches the schema.\n\n"
    "Rules:\n"
    "- Output JSON ONLY. No prose, no markdown fences, no commentary.\n"
    "- Use null for fields genuinely absent from the content.\n"
    "- Coerce types per the schema (e.g. number from \"$45.00\" → 45.00).\n"
    "- Don't fabricate values not supported by the content."
)

# Some models still wrap output in code fences despite explicit
# instructions — strip them defensively.
_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\n?```\s*$")


def _strip_fence(text: str) -> str:
    text = text.strip()
    text = _FENCE_OPEN_RE.sub("", text)
    text = _FENCE_CLOSE_RE.sub("", text)
    return text.strip()


def _build_user_message(
    content: str,
    schema: dict[str, Any],
    instruction: str | None,
) -> str:
    parts: list[str] = []
    if instruction:
        parts.append(f"Extraction instruction: {instruction}")
    parts.append("JSON Schema:\n" + json.dumps(schema, indent=2))
    parts.append("Page content:\n" + content[:MAX_CONTENT_CHARS])
    return "\n\n".join(parts)


async def extract_structured_data(
    page_content: str,
    schema: dict[str, Any],
    instruction: str | None,
    anthropic_client,  # AnthropicClient (kept untyped here to avoid a circular import)
) -> dict[str, Any]:
    """Send page content + schema to Claude, return parsed JSON.

    Raises RuntimeError on:
      - upstream Anthropic error (propagated message)
      - Claude returns non-JSON we can't repair
    """
    user_message = _build_user_message(page_content, schema, instruction)
    raw = await anthropic_client.messages(
        system_prompt=EXTRACT_SYSTEM_PROMPT,
        user_message=user_message,
        max_tokens=4000,
    )
    cleaned = _strip_fence(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude returned non-JSON after fence-strip: {e}; "
            f"first 200 chars: {cleaned[:200]!r}"
        )
