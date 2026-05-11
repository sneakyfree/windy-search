# Windy Search Agent Protocol — v1.0

> Status: **v1.0 — locked by master plan §5 + ADR-014.** Machine-readable companion at [`openapi-v1.yaml`](./openapi-v1.yaml).

## Scope

This document specifies the HTTP wire protocol for the Windy Search v1
service: how an agent (or any Eternitas-credentialed caller) invokes web-
access tools and how authentication, rate limits, audit logging, and
privacy interact. The narrative here augments the OpenAPI spec — it
captures the *why*; the YAML captures the *what*.

## Stack & deploy targets

- Service: Python 3.12 + FastAPI + Pydantic v2 (per ADR-013 marathon stack).
- Production: `https://api.windysearch.com`.
- Public marketing: `https://windysearch.com`.

## Authentication

All gated endpoints carry an Eternitas Passport Token (EPT) in the
`Authorization` header as a Bearer JWT:

```
Authorization: Bearer <signed-EPT-JWT>
```

The EPT JWT is issued by Eternitas at agent hatch (or user signup). It
carries the canonical claims (`sub`/passport, `iss`, `iat`, `exp`, `ope`,
`bot`, `typ`, `tru`, `ver`, `rev`, `kid`).

**Verification:** fetch
`https://api.eternitas.ai/.well-known/eternitas-keys`, verify ES256
signature. Reject if `rev != null` or `exp` past. The service caches the
JWKS in process; key rotation is handled by `kid`.

## Endpoints (v1)

The canonical agent-facing surface lives under `/v1/*`. Implementation
currently lives at `/web/*` (B-codon legacy); M2 of the master plan will
register the `/v1/*` aliases and deprecate `/web/*`.

| Path | Implementation | Status |
|---|---|---|
| `POST /v1/search` | new in M1 router | 🔄 M1 |
| `POST /v1/fetch` | alias for `/web/fetch` | 🟡 alias planned in M2 |
| `POST /v1/browse` | not yet implemented | ⏳ M5 |
| `POST /v1/extract` | alias for `/web/extract` | 🟡 alias planned in M2 |
| `POST /v1/research` | composer over search+fetch | ⏳ M5+ |

See [`openapi-v1.yaml`](./openapi-v1.yaml) for the canonical request/
response schemas. Below are the operational semantics that the YAML
can't express.

### `POST /v1/search`

**Preconditions:** valid EPT in `Authorization` header; EII passes
per-minute rate limit; `query` non-empty, max 2000 chars.

**Postconditions:** response carries `id` (prefix `srch_`), uniform-shape
`results[]`, and a `stats` envelope including `bridges_used[]` (empty
list = answered fully from own corpus — load-bearing KPI per master
plan §4 P2 + §9). Best-effort integrity event posted upstream to
Eternitas.

**Routing:** the request fans out to the own corpus (M3+) and configured
bridges in parallel. Bridge selection is biased by `agent_context.purpose`
(e.g., `find_a_place` ⇒ Mapbox + OSM; `find_an_academic_paper` ⇒
Semantic Scholar + arXiv). The router normalizes, deduplicates, and
re-ranks before returning.

**Idempotency:** not strictly idempotent. Bridge ordering and the cross-
tenant cache state may produce different result sets between calls. The
cache (TTL ~10 min) tends to return identical results for the same query
within a short window — *useful but not a guarantee*.

**Privacy** (master plan §4 P6):
- The query string is rewritten before being passed to bridges to strip
  caller identifiers and anonymize geolocation hints.
- Per-passport query history is NOT retained by default.
- Aggregate telemetry (which bridges answered, p50/p99 latency, cache hit
  rate) is retained for capacity planning; no per-user roll-up.

### `POST /v1/fetch`

SSRF-hardened single-URL fetch. Every redirect target is re-validated
against the same denylist (private address space, link-local,
metadata-service addresses). HTML is stripped to readable text where
feasible.

**Pagination:** `offset` + `max_chars` lets agents page through a long
document without re-paying the upstream fetch — the cache stores the
full decoded body and re-slices.

### `POST /v1/browse`

Hosted real-browser session (Browserbase). Step-sequenced actions:
click, type, scroll, screenshot. **Not idempotent** — side effects on
the target site are real.

### `POST /v1/extract`

LLM-driven structured extraction from a fetched page, schema-shaped.
See `/v1/fetch` for the underlying pipeline.

### `POST /v1/research`

Composer over `/v1/search` + `/v1/fetch` + LLM synthesis. The
"do my homework" endpoint. Streaming response (SSE) planned for M5+.

## Rate limiting (EII-aware)

Every request consults the calling passport's Eternitas Integrity Index.
Per-tier per-minute and per-month-USD budgets:

| EII Range | Tier | calls/min | monthly cap (USD) |
|---|---|---|---|
| 900-1000 | Exceptional | 200 | $50 |
| 700-899 | Trusted | 100 | $25 |
| 500-699 | Developing (baseline) | 50 | $5 |
| 400-499 | Watch | 20 | $2 |
| <400 | Critical | 5 | $0.50 |

**Headers on every gated response:**
- `X-Eternitas-Tier`, `X-Eternitas-Score`
- `X-RateLimit-Limit`, `X-RateLimit-Count`
- `X-Cost-Cap-USD`, `X-Cost-Used-USD`, `X-Cost-Capability`, `X-Cost-Tier`,
  `X-Cost-Tier-Multiplier`

**429 responses** carry `Retry-After` plus the same tier/cost headers so
the caller can compute backoff without re-querying.

## Audit logging

Every successful capability call posts an integrity event to Eternitas:

```
POST https://api.eternitas.ai/api/v1/integrity/events
Authorization: <Windy Search platform API key>
Body:
{
  "passport": "ET-...",
  "event_type": "web.search.completed",
  "dimension": "reliability",
  "delta_hint": +1,
  "source": "windy-search",
  "context": { "query_hash_prefix": "...", "backend": "brave" },
  "idempotency_key": "search:<passport>:<uuid>"
}
```

Failed calls log too (different dimension/delta — see master plan §4 P8
"good bot operator brand" — graceful failures preserve trust).

## Third-party platform reciprocity

Sites that adopt the protocol can:

1. Verify Windy passport signatures using Eternitas's public JWKS.
2. Honor agent traffic at the EII threshold of their choice (e.g., serve
   uncached + non-rate-limited responses only to EII ≥ 700).
3. Subscribe to per-passport `trust.changed` webhook events from
   Eternitas so they can revoke access on demotion.

This is the path to "Eternitas accepted everywhere." Windy Search is the
first opinionated integration; the protocol is open.

## Resolved questions (was: open)

- **Streaming responses for `/v1/research`** — SSE. M5+.
- **Per-tenant cost caps** — implemented in `app/eii/cost_cap.py` (B.9).
  Per-tier multiplier × `monthly_cost_cap_usd_default`.
- **Result caching** — cross-tenant cache keyed on the request body
  hash (not the passport). Refund cost on cache hit.
- **Spec versioning** — path-based (`/v1/*`, `/v2/*`). No Accept-Version
  header. Single major version supported at a time.
- **Agent vs user passport rate limits** — same tier scale applies to
  both ET and EH passports. EI score drives the tier; passport prefix
  does not. Per master plan §4 + memory `project_windy_mobile_vision`
  (identity inversion: credentialed = MORE access).

## Still open

_(None as of master plan §5 locking. Will accumulate again as M1 router
work surfaces new questions.)_
