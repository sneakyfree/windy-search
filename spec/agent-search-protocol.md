# Windy Search Agent Protocol — v0.1 (DRAFT)

> Status: drafting — captures the vision discussion from 2026-05-06. Subject to revision.

## Scope

This document specifies the HTTP wire protocol for the Windy Search v1 service: how an agent (or any Eternitas-credentialed caller) invokes web access tools and how authentication, rate limits, and audit logging interact.

## Authentication

All requests carry an Eternitas Passport Token (EPT) in the Authorization header:

```
Authorization: Eternitas <signed-EPT-JWT>
```

The EPT JWT is issued by Eternitas at agent hatch (or user signup). It carries:
- `sub` — passport (e.g. `ET-XXXXX...` for agents, `EH-...` for humans)
- `iss` — `eternitas.ai`
- `iat`, `exp` — issued/expires
- `ope` — owner operator (the human who controls this passport)
- `bot` — bot name (for agent passports)
- `typ` — bot type
- `tru` — trust score (0-100, legacy, mirrored from EII overall)
- `ver` — verification tier
- `rev` — revocation status
- `kid` — JWKS key ID for offline verification

Verification: fetch `https://api.eternitas.ai/.well-known/eternitas-keys`, verify ES256 signature. Reject if `rev != null` or `exp` past.

## Endpoints (v1)

### `POST /v1/web/search`

Search the open web. Returns top-N results.

**Request:**
```json
{
  "query": "Austin TX VA loan officer market 2024",
  "num_results": 10
}
```

**Response:**
```json
{
  "results": [
    {
      "url": "https://...",
      "title": "...",
      "snippet": "...",
      "rank": 1
    }
  ],
  "metadata": {
    "backend": "brave",
    "cost_usd": 0.003,
    "ms_elapsed": 412
  }
}
```

### `POST /v1/web/fetch`

Download a URL; return clean readable text (no chrome, no nav).

**Request:**
```json
{
  "url": "https://...",
  "mode": "readability"
}
```

**Response:**
```json
{
  "url": "https://...",
  "title": "...",
  "text": "Clean article body...",
  "links": ["https://..."],
  "metadata": {
    "fetched_at": "2026-05-06T...",
    "ms_elapsed": 234
  }
}
```

### `POST /v1/web/browse`

Drive a real browser session — clicks, forms, screenshots. Used for sites that require interaction or block bare HTTP.

**Request:**
```json
{
  "url": "https://...",
  "instructions": "Click the 'Sign In' button, fill the form with...",
  "max_steps": 20
}
```

**Response:**
```json
{
  "final_url": "https://...",
  "screenshots": ["base64..."],
  "actions_taken": [
    { "step": 1, "action": "click", "target": "...", "result": "..." }
  ],
  "extracted_text": "...",
  "metadata": {
    "backend": "browserbase",
    "cost_usd": 0.18,
    "ms_elapsed": 14820
  }
}
```

### `POST /v1/web/extract`

LLM-driven structured data extraction from a URL or screenshot.

**Request:**
```json
{
  "url": "https://...",
  "schema": {
    "name": "string",
    "phone": "string",
    "email": "string",
    "specialties": "array<string>"
  }
}
```

**Response:**
```json
{
  "data": {
    "name": "Bob LastName",
    "phone": "+1...",
    "email": "bob@...",
    "specialties": ["VA", "FHA"]
  },
  "confidence": 0.87,
  "metadata": {
    "model": "claude-sonnet",
    "cost_usd": 0.04
  }
}
```

### `POST /v1/web/research`

Higher-order composer: search + fetch top-N + synthesize. The "do my homework" endpoint.

**Request:**
```json
{
  "topic": "Austin TX loan officer market for VA loan specialists",
  "depth": "comprehensive",
  "max_sources": 10
}
```

**Response:**
```json
{
  "synthesis": "Long-form synthesis...",
  "sources_consulted": [
    { "url": "...", "title": "...", "weight": 0.34 }
  ],
  "metadata": {
    "total_cost_usd": 0.42,
    "ms_elapsed": 28430
  }
}
```

## Rate limiting (EII-aware)

Every request consults the calling passport's Eternitas Integrity Index. Default per-tier policy:

| EII Range | Band | calls/min | calls/day (web.search) | calls/day (web.browse) |
|---|---|---|---|---|
| 900-1000 | Exceptional | 200 | 5000 | 1000 |
| 750-899 | Good | 100 | 2000 | 500 |
| 600-749 | Fair | 50 | 500 | 100 |
| 400-599 | Poor (incl cold-start) | 20 | 100 | 20 |
| <400 | Critical | 5 | 20 | 5 |

Rate limit responses: `429 Too Many Requests` with `X-RateLimit-Reset` and `X-Eternitas-Score` headers.

## Audit logging

Every successful request POSTs an event to Eternitas:

```
POST https://api.eternitas.ai/api/v1/integrity/events
Authorization: <Windy Search service API key>
Body: {
  "passport": "ET-...",
  "event": "web.search",
  "outcome": "success",
  "dimension": "reliability",
  "delta_hint": +1,
  "source": "windy-search",
  "idempotency_key": "..."
}
```

Failed requests log too (different dimension/delta).

## Future: third-party platform reciprocity

Sites that adopt the protocol can:

1. Verify Windy passport signatures using Eternitas's public JWKS
2. Honor agent traffic at the EII threshold of their choice
3. Receive per-passport webhook subscriptions for trust-changes (`trust.changed` event already fires from Eternitas today)

This is the path to "Eternitas accepted everywhere." Windy Search is the first opinionated integration; the protocol is open.

## Open questions / TBD

- Streaming responses for `web.research` (SSE? chunked?)
- Per-tenant cost caps (prevent a single user's agent from runaway spend)
- Result caching policy (private to passport vs cross-tenant via hash)
- Spec-versioning policy (`/v1/...` vs Accept-Version header)
- "Agent Mode" vs "User Mode" — does a human's EH passport have different rate limits than an agent's ET passport?
