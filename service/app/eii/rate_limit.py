"""Per-passport sliding-window rate limiter (Phase B.3).

Uses Redis sorted sets keyed by passport — one entry per request,
score = unix-millis timestamp. On each call we trim entries older than
60s, ZADD the new one, ZCARD to count, and EXPIRE the key to keep
storage bounded.

Fails open when Redis is unavailable: the route still completes, but
no enforcement happens. Matches eternitas's main.py:144-146 posture —
the operator's choice is throughput over hardening when Redis is down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    count: int
    limit: int
    tier_name: str


WINDOW_MS = 60_000  # 1 minute
KEY_PREFIX = "rl:windy-search:passport"


def _key(passport: str) -> str:
    return f"{KEY_PREFIX}:{passport}"


async def check(
    redis: Optional[aioredis.Redis],
    passport: str,
    limit_per_minute: int,
    tier_name: str,
) -> RateLimitDecision:
    """Decide whether this request fits the passport's per-minute budget."""
    if redis is None:
        # Fail open — surfaced via the response header so callers can
        # distinguish "no enforcement" from "enforcement says ok."
        return RateLimitDecision(allowed=True, count=0, limit=limit_per_minute, tier_name=tier_name)

    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - WINDOW_MS
    key = _key(passport)
    member = f"{now_ms}:{id(object())}"  # unique per call within the same ms

    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, 0, cutoff_ms)
    pipe.zadd(key, {member: now_ms})
    pipe.zcard(key)
    pipe.expire(key, 65)
    results = await pipe.execute()
    count = int(results[2])

    return RateLimitDecision(
        allowed=count <= limit_per_minute,
        count=count,
        limit=limit_per_minute,
        tier_name=tier_name,
    )
