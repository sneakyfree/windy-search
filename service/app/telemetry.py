"""Fire-and-forget event emission to Windy Admin (ADR-WA-001).

Telemetry must NEVER affect product traffic: posts run as background
tasks with a short timeout, every error is swallowed (debug-logged),
and the module is inert unless both WINDY_ADMIN_INGEST_URL and
WINDY_ADMIN_INGEST_TOKEN are configured.

Privacy hard line (ADR-WA-001 §4): envelopes carry counts, costs,
durations, and models only — never message content. The ingest rejects
content-like metadata keys with 422; keep it that way by fixing the
emitter, not the guard.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
# Strong refs so fire-and-forget tasks aren't garbage-collected mid-flight.
_inflight: set[asyncio.Task] = set()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=2.0)
    return _client


async def _send(events: list[dict]) -> None:
    settings = get_settings()
    try:
        resp = await _get_client().post(
            f"{settings.windy_admin_ingest_url.rstrip('/')}/v1/events",
            json={"events": events},
            headers={"Authorization": f"Bearer {settings.windy_admin_ingest_token}"},
        )
        if resp.status_code != 202:
            logger.debug("telemetry ingest returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001 — telemetry never raises
        logger.debug("telemetry post failed: %s", e)


def emit(
    event_type: str,
    *,
    actor_type: str,
    actor_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_microcents: int | None = None,
    duration_ms: int | None = None,
    session_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Queue one envelope for delivery; a no-op unless configured."""
    settings = get_settings()
    if not (settings.windy_admin_ingest_url and settings.windy_admin_ingest_token):
        return
    event = {
        "ts": datetime.now(UTC).isoformat(),
        "platform": "windy-search",
        "service": "search-api",
        "event_type": event_type,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "model": model,
        "provider": provider,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_microcents": cost_microcents,
        "duration_ms": duration_ms,
        "session_id": session_id,
        "metadata": metadata or {},
    }
    try:
        task = asyncio.get_running_loop().create_task(_send([event]))
    except RuntimeError:
        return  # no running loop (sync context) — drop rather than block
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
