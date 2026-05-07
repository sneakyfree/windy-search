"""Inbound webhook consumer with HMAC verification.

Eternitas dispatches firehose events to every registered platform with
two signatures:

    X-Eternitas-Signature: sha256=<hex>   HMAC-SHA256 over raw body
    X-Windy-Signature:     <detached JWS>  ES256 over raw body

We verify the HMAC using the webhook_secret eternitas issued when we
registered. Any mismatch returns 401 — but the response always 204s
to the dispatcher so it can't tell from outside whether handling
succeeded (defense in depth).

Event handlers:
  integrity.event  → invalidate the cached EII score for that passport
                     so the next /web/* request from that agent picks
                     up the new tier within seconds, not minutes.

Other event types are logged + acked. Future codons add handlers
(passport.revoked → blacklist; clearance.demoted → tier override; etc).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

logger = logging.getLogger(__name__)


def verify_signature(payload_bytes: bytes, signature_header: str | None, secret: str) -> bool:
    """Constant-time HMAC verification.

    `signature_header` is the raw value of `X-Eternitas-Signature`,
    expected to be `sha256=<hex>`. Returns False on any malformed input.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    received_hex = signature_header[len("sha256="):]
    expected_hex = hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(received_hex, expected_hex)


async def handle_event(event_type: str, payload: dict[str, Any], app_state: Any) -> None:
    """Dispatch a verified event to its handler. No-op on unknown types."""
    if event_type == "integrity.event":
        await _on_integrity_event(payload, app_state)
        return

    if event_type in ("passport.revoked", "passport.suspended"):
        # Could blacklist the passport in a local cache to fast-fail
        # gated routes. Future codon — for now just log so we have
        # operational visibility that revocations are propagating.
        logger.info(
            "received %s for passport %s — no handler yet",
            event_type, payload.get("passport_number") or payload.get("passport"),
        )
        return

    logger.debug("received unhandled event_type=%s", event_type)


async def _on_integrity_event(payload: dict[str, Any], app_state: Any) -> None:
    """Invalidate the cached EII score so the next gated request for
    this passport re-fetches the fresh score from eternitas."""
    passport = payload.get("passport")
    if not passport:
        logger.warning("integrity.event payload missing 'passport': %s", payload)
        return

    score_cache = getattr(app_state, "score_cache", None)
    if score_cache is None:
        return

    score_cache.invalidate(passport)
    logger.debug("invalidated score cache for %s after integrity.event", passport)
