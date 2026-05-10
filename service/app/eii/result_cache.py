"""Cross-tenant result cache (Phase B.10).

Two agents asking Brave the same thing within an hour shouldn't cost
us two queries. This module hashes the canonical request input
(query+limit for search, url+offset+max_chars for fetch) and stores
the result keyed by that hash, namespaced per capability.

Privacy note: we cache by query *hash*, not by passport. Different
agents share cache entries — that's the point. Their integrity events
still record per-passport, so audit trail is unaffected. The cache key
never embeds the passport so a hostile passport can't poison entries
for other agents either.

Cache hits are accompanied by a cost-cap refund (see app/eii/cost_cap.py)
so a passport's monthly budget reflects real spend, not theoretical.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# TTLs per capability. Search results turn over fast (news, prices,
# rankings); fetched pages tend to be more stable.
TTL_SECONDS: dict[str, int] = {
    "web.search":  3600,        # 1 hour
    "web.fetch":   86400,       # 24 hours
    "web.browse":  3600,        # browse results are session-bound — keep short
    "web.extract": 86400,       # extraction is deterministic given input
}

DEFAULT_TTL_SECONDS = 3600

KEY_PREFIX = "cache:windy-search"


def _hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _key(capability: str, payload: dict[str, Any]) -> str:
    return f"{KEY_PREFIX}:{capability}:{_hash(payload)}"


async def get_cached(
    redis: aioredis.Redis | None,
    capability: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the cached value as a dict, or None on miss / Redis-unavailable."""
    if redis is None:
        return None
    try:
        raw = await redis.get(_key(capability, payload))
    except Exception as e:
        logger.warning("cache get failed for %s: %s", capability, e)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw if isinstance(raw, str) else raw.decode())
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning("cache decode failed for %s: %s", capability, e)
        return None


async def set_cached(
    redis: aioredis.Redis | None,
    capability: str,
    payload: dict[str, Any],
    value: dict[str, Any],
    ttl_seconds: int | None = None,
) -> None:
    """Store the value with the per-capability TTL. Best-effort."""
    if redis is None:
        return
    ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else TTL_SECONDS.get(capability, DEFAULT_TTL_SECONDS)
    )
    try:
        await redis.set(_key(capability, payload), json.dumps(value), ex=ttl)
    except Exception as e:
        logger.warning("cache set failed for %s: %s", capability, e)
