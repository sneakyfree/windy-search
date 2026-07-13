"""Ops event ring — ADR-060 `get_logs`, content-free BY CONSTRUCTION.

Replicates the windy-mind pattern (the Class-C template): entries are
`{ts, level, event, code?}` where `event` is a fixed vocabulary and `code`
a short enum/identifier (source name, error category). Raw error strings
are CATEGORIZED here, never stored — bridge error bodies can echo the
query text, and the privacy hard line is that no user/agent content ever
reaches an ops surface.
"""
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

OPS_LOG_MAX = 500

_ring: deque[dict[str, Any]] = deque(maxlen=OPS_LOG_MAX)


def ops_log(level: str, event: str, code: str | int | None = None) -> None:
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "level": level,
        "event": event,
    }
    if code is not None:
        entry["code"] = code
    _ring.append(entry)


def entries() -> list[dict[str, Any]]:
    return list(_ring)


def reset_for_tests() -> None:
    _ring.clear()


def categorize_status(status_code: int) -> str:
    """Map a bridge HTTP failure to a fixed, content-free category."""
    if status_code == 429:
        return "quota_429"
    if status_code in (401, 402, 403):
        return "auth_or_billing"
    if status_code >= 500:
        return "upstream_5xx"
    return f"http_{status_code}"
