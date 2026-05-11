"""Privacy proxy — query rewriting before external bridge calls (M1.9).

Strips PII patterns from the query that will be passed to external
bridges (Brave, Google, etc.). The ORIGINAL query stays in-process for
the ranker — BM25 still scores candidates against the user's intent
without exposing PII outside our service boundary.

Patterns redacted at V1:
  * Email addresses           ⇒ `<redacted-email>`
  * Phone numbers (US-ish)    ⇒ `<redacted-phone>`
  * SSN-shaped digit groups   ⇒ `<redacted-ssn>`
  * Lat/long-precision coords ⇒ rounded to 2 decimal places (~1km)

Deliberately NOT redacted at V1 (false-positive risk too high):
  * Street addresses — would need NER; defer to M5+
  * Names — only the user's own; no way to tell from query
  * Credit cards — Luhn check needed; defer until we see real signal
  * Free-text geographic descriptions ("near my house") — agent intent

Per master plan §4 P6 + §6 M1.9 + ADR-014.

The function returns a (sanitized, n_redactions) tuple so the router
can log per-request redaction counts as aggregate-only telemetry.
"""
from __future__ import annotations

import re

# `\b` word boundaries keep these from gobbling adjacent punctuation.

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# US-ish phone numbers: 10 digits with optional +1, optional separators
# (spaces/dots/dashes), optional parens around the area code.
PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"
)

# SSN literal pattern: 9 digits split by dashes. Without dashes a 9-digit
# number could be many things (zip+4, order id, etc.); we only redact
# the dashed form to keep false positives low.
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# High-precision lat/long: any signed decimal with 3+ decimal places.
# Rounded to 2 places (~1.1 km at the equator) — preserves the agent's
# "find places near here" intent without identifying a specific home.
COORD_RE = re.compile(r"-?\d+\.\d{3,}")

REDACTED_EMAIL = "<redacted-email>"
REDACTED_PHONE = "<redacted-phone>"
REDACTED_SSN = "<redacted-ssn>"


def rewrite_query(query: str) -> tuple[str, int]:
    """Sanitize PII from `query` before sending to external bridges.

    Returns `(sanitized_query, n_redactions)` — the count is for
    aggregate-only telemetry (per master plan §4 P6 "no per-user
    query history"). When the count is 0 the sanitized query equals
    the input.

    Idempotent: a sanitized query passed back through this function
    is unchanged.
    """
    sanitized = query
    count = 0

    sanitized, n = EMAIL_RE.subn(REDACTED_EMAIL, sanitized)
    count += n

    sanitized, n = PHONE_RE.subn(REDACTED_PHONE, sanitized)
    count += n

    sanitized, n = SSN_RE.subn(REDACTED_SSN, sanitized)
    count += n

    def _round_coord(m: re.Match[str]) -> str:
        return f"{float(m.group(0)):.2f}"

    sanitized, n = COORD_RE.subn(_round_coord, sanitized)
    count += n

    return sanitized, count
