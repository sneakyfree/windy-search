"""Cross-source URL deduplication (M1.6).

When the same URL surfaces from multiple sources — e.g., own corpus +
Brave both index the same news article — we collapse to a single result.
The surviving entry is the one from the LOWEST-priority source, which
per the `Source` convention means "most preferred" (own_corpus(0) wins
over brave(10) wins over google(30)).

URL canonicalization is deliberately conservative — only normalizations
that virtually never change resource identity:

  * lowercase scheme + host
  * strip default ports (`:80` for http, `:443` for https)
  * strip URL fragment (`#section`)
  * strip trailing slash on non-root paths

Query parameters are kept as-is — sorting them risks breaking signed
URLs, and the dedup miss for differently-ordered params is rare and
benign. Stricter canonicalization (e.g., utm_* stripping) can land in
M1+ once we have telemetry showing the dedup miss rate.

Per master plan §6 M1.6 + ADR-014.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from urllib.parse import urlparse, urlunparse

from app.sources.base import RawResult, Source


def canonical_url(url: str) -> str:
    """Return a canonical form of `url` for dedup-key purposes.

    Does NOT modify query params (see module docstring for rationale).
    Returns the input unchanged if it doesn't parse — defensive against
    malformed strings; the dedup table will then key on the literal
    string.
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return url

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, parts.params, parts.query, ""))


def url_hash(url: str) -> str:
    """SHA-256 hex digest of the canonical URL. 64-char dedup key."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def dedup_across_sources(
    pairs: Sequence[tuple[Source, RawResult]],
) -> list[tuple[Source, RawResult]]:
    """Collapse duplicates by canonical URL.

    When the same URL appears from multiple sources, the entry from the
    LOWEST-priority source wins (most-preferred source). Stable: among
    survivors, input order is preserved.

    Stats: dropped duplicates don't appear in `bridges_used` — the
    router's stats are computed post-merge on the final results list,
    so a brave result collapsed by an own-corpus result correctly
    excludes brave from bridges_used.
    """
    if not pairs:
        return []

    # First pass — find the best (lowest) priority for each URL hash.
    best_priority: dict[str, int] = {}
    for source, raw in pairs:
        h = url_hash(raw.url)
        if h not in best_priority or source.priority < best_priority[h]:
            best_priority[h] = source.priority

    # Second pass — keep the first occurrence at the best priority.
    seen: set[str] = set()
    out: list[tuple[Source, RawResult]] = []
    for source, raw in pairs:
        h = url_hash(raw.url)
        if h in seen:
            continue
        if source.priority != best_priority[h]:
            continue
        seen.add(h)
        out.append((source, raw))
    return out
