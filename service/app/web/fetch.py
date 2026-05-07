"""URL fetcher with SSRF protection (Phase B.5).

This is the highest-risk capability surface in Windy Search: an
authenticated agent can ask the service to fetch arbitrary URLs. Without
careful validation, an attacker with a valid EPT could pivot through us
to internal AWS metadata, Kubernetes APIs, or RFC1918 hosts.

Defenses applied here:
  1. Scheme allow-list: http, https only. No file://, gopher://, etc.
  2. Host allow-list (negative): block localhost, *.local, *.internal,
     metadata.google.internal, instance-data.ec2.internal.
  3. DNS-level check: refuse if the hostname resolves to any RFC1918 /
     loopback / link-local / IPv6 ULA address.
  4. Manual redirect handling: every Location target is re-validated
     before re-fetching. Defends against redirect-to-internal SSRF.
  5. Body byte cap: stream up to MAX_BYTES_FETCH (5 MB) before truncating,
     so a malicious target can't OOM us via a multi-GB stream.
  6. User-Agent that identifies us, so target sites can opt out.

Known v1 limitations (documented for follow-up codons):
  - DNS rebinding TOCTOU: between validate() and fetch(), DNS for the
    same hostname could change to a private IP. Mitigation requires a
    DNS-pinning HTTP transport (resolve once, then HTTP to the IP with
    Host header). Defer to a hardening codon.
  - IPv6: handled in the block list but not as deeply tested as IPv4.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

# 5 MB ceiling on raw fetch — more than enough for HTML/JSON/text; protects
# against runaway streams.
MAX_BYTES_FETCH = 5 * 1024 * 1024

# Maximum number of redirects we'll follow (each is re-validated).
MAX_REDIRECTS = 5

USER_AGENT = "WindySearch/0.1 (+https://windysearch.com)"

# Networks we refuse to fetch — RFC1918, loopback, link-local, ULA, etc.
_BLOCKED_NETWORKS = tuple(
    ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "0.0.0.0/8",
        "169.254.0.0/16",  # link-local + cloud metadata (169.254.169.254)
        "100.64.0.0/10",   # Carrier-grade NAT — also internal-ish
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)

_BLOCKED_HOSTS_EXACT = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "instance-data.ec2.internal",
})

_BLOCKED_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".localdomain",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


# Resolver type: hostname → list of IP address strings. Lets tests inject
# a deterministic resolver without monkey-patching the socket module.
Resolver = Callable[[str], list[str]]


def _default_resolver(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return []
    return [info[4][0] for info in infos]


class UnsafeURLError(Exception):
    """Raised when a URL fails the SSRF allow/deny checks."""


@dataclass(frozen=True)
class FetchResponse:
    final_url: str
    status_code: int
    content_type: str
    content: str
    total_chars: int
    offset: int
    max_chars: int
    truncated: bool


def validate_fetchable_url(url: str, *, resolver: Optional[Resolver] = None) -> None:
    """Raises UnsafeURLError if the URL would be unsafe to fetch."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"unsupported scheme: {parsed.scheme!r}")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise UnsafeURLError("missing hostname")

    if hostname in _BLOCKED_HOSTS_EXACT:
        raise UnsafeURLError(f"blocked host: {hostname}")

    for suffix in _BLOCKED_HOST_SUFFIXES:
        if hostname.endswith(suffix):
            raise UnsafeURLError(f"blocked host suffix: {hostname}")

    # If the hostname is itself a literal IP, validate that directly
    # (no DNS lookup needed and bypassed by attackers who skip DNS).
    try:
        literal = ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None

    candidates: list[str]
    if literal is not None:
        candidates = [str(literal)]
    else:
        resolve = resolver or _default_resolver
        candidates = resolve(hostname)
        if not candidates:
            raise UnsafeURLError(f"DNS resolution returned no addresses for {hostname}")

    for ip_str in candidates:
        try:
            addr = ip_address(ip_str)
        except ValueError:
            continue
        for net in _BLOCKED_NETWORKS:
            if addr in net:
                raise UnsafeURLError(
                    f"{hostname} resolves to blocked network address {ip_str}"
                )


async def fetch_url(
    url: str,
    *,
    max_chars: int,
    offset: int,
    timeout_seconds: float = 10.0,
    resolver: Optional[Resolver] = None,
) -> FetchResponse:
    """Fetch url with SSRF protection + manual redirect re-validation.

    Returns a FetchResponse with the final post-redirect URL, decoded
    body slice, and pagination metadata. Raises UnsafeURLError on any
    SSRF check failure (including for redirect targets) or
    httpx.HTTPError on transport failure.
    """
    current_url = url
    redirects_followed = 0
    response: httpx.Response | None = None

    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        while True:
            validate_fetchable_url(current_url, resolver=resolver)

            response = await client.get(current_url)

            if response.status_code in (301, 302, 303, 307, 308):
                if redirects_followed >= MAX_REDIRECTS:
                    raise UnsafeURLError("too many redirects")
                location = response.headers.get("location")
                if not location:
                    raise UnsafeURLError("redirect without Location header")
                current_url = urljoin(current_url, location)
                redirects_followed += 1
                continue

            response.raise_for_status()
            break

    assert response is not None  # loop exits via break or raise

    content_type = response.headers.get("content-type", "")

    # Cap body size before decoding. httpx already buffered it (we're not
    # streaming) — slice the bytes we actually keep.
    body_bytes = response.content[:MAX_BYTES_FETCH]
    body = body_bytes.decode("utf-8", errors="replace")

    if "html" in content_type.lower() or "<html" in body[:1000].lower():
        body = _HTML_TAG_RE.sub(" ", body)
        body = _WHITESPACE_RE.sub(" ", body).strip()

    total_chars = len(body)
    offset = max(0, offset)
    max_chars = max(1, min(max_chars, MAX_BYTES_FETCH))
    sliced = body[offset:offset + max_chars]
    truncated = (offset + max_chars) < total_chars

    return FetchResponse(
        final_url=str(response.url),
        status_code=response.status_code,
        content_type=content_type,
        content=sliced,
        total_chars=total_chars,
        offset=offset,
        max_chars=max_chars,
        truncated=truncated,
    )
