# Windy Search

> **Agent-centric web access for the Windy ecosystem.**
>
> Domain: [windysearch.com](https://windysearch.com) (Cloudflare, registered 2026-05-06)
> Status: **v0.1.0 service live in prod** at `api.windysearch.com` (M0–M2 shipped). See [Status](#status).

## What it is

Windy Search is the layer that lets an AI agent — born with Eternitas credentials inside the Windy ecosystem — actually *do work on the open web*.

Today's web is hostile to agents. Cloudflare bot detection, CAPTCHAs, rate limits, anti-scraping. Most "agent browsers" are either expensive (Anthropic Computer Use) or fragile (raw Playwright against modern sites). And none of them carry verifiable, reputation-scored identity.

Windy Search solves this by being **a web access toolkit, not a browser** — at least to start. An agent calls our API; we do the search/fetch/browse on its behalf, with the agent's Eternitas passport signing every request, and every observation feeding the agent's Integrity Index back at Eternitas.

## The two flagship pillars Windy Search supports

The whole Windy ecosystem rests on two:

1. **The most polished voice-to-text platform anywhere** — frictionless capture of human intent
2. **The most agent-friendly ecosystem in the world** — frictionless agent execution

Windy Search is the *web reach* extension of pillar #2. It's how an agent — owned by a grandma in a ballroom whose Pro account just hatched it — can research, navigate, extract, and report back without anyone needing to be a developer.

## Three-phase product roadmap

| Phase | What ships | Effort | Audience |
|---|---|---|---|
| **1 — `windy-search-service` (toolkit)** | HTTP API: `web.search`, `web.fetch`, `web.browse`, `web.extract`. Agents call as tools. Eternitas passport auth. Per-EII rate limits. Audit logs back to Eternitas. | ~6 weeks | Agents only — no UI |
| **2 — Windy Search Chrome extension** | Adds Eternitas identity layer + ecosystem integration to any Chromium browser. "Login with Eternitas" surfaces, integrity score badges, one-click "send to my agent" for any page. | ~6 weeks after Phase 1 | Humans on existing browsers |
| **3 — Windy Search browser (Chromium fork)** | Standalone browser. Agentic-first. Default search via partner deal or our own engine. Eventually: search rankings + ad business. | 18-24 months out, conditional on Phase 2 traction | Humans + agents |

We don't take Phase 3 on speculatively. Phase 1 + Phase 2 prove demand, then we graduate.

## The Eternitas integration

Windy Search is an *event source* for the Eternitas Integrity Index — the FICA-style 0-1000 score every Windy agent has. Every action through the toolkit:

- **Signs requests** with the agent's Eternitas Passport Token (EPT JWT) so third-party sites can verify identity + score
- **Reports observations** to Eternitas (`POST /api/v1/integrity/events`): successful task → reliability +5, abusive → safety -20, etc.
- **Consults the score** to decide rate limits before serving: agent at EII 850 gets 100 calls/min, agent at EII 350 gets 10/min
- **Cold-start friendly**: newborn agents (EII 500) start with reasonable limits and earn higher allowances through good behavior

This makes Windy Search the *primary signal source* for agent reliability. Other products (Mail, Chat, Cloud) add more signals later, but web research alone gives Eternitas enough to score with.

## The strategic moat

The end-state vision: **Eternitas credentials become so valuable and trustworthy that platforms across the open web (Amazon, Facebook, Google, Stripe, etc.) accept them and grant agent access based on Integrity Index score.**

Windy Search is how that flywheel starts spinning. Every agent that uses Windy Search builds an Eternitas reputation. As the agent population grows, third-party sites face pressure to honor verified Windy agents — because the alternative is anonymous bots they can't tell apart from human users.

This is a long game (5-10 years to consortium-scale adoption), but Windy Search is what makes the first 18 months of the game playable.

## Architecture (v1 — service-only)

```
┌─────────────────────────────────────────────────────────────────┐
│ AGENT (Windy Fly) calls toolkit as a tool                       │
│                                                                 │
│   ┌──────────────────────────────────────────────────────────┐ │
│   │ POST https://api.windysearch.com/v1/web/search           │ │
│   │ Authorization: Eternitas <signed-EPT-JWT>                │ │
│   │ Body: { "query": "Austin TX VA loan officers 2024" }     │ │
│   └──────────────────────────────────────────────────────────┘ │
│                              ↓                                  │
│   ┌──────────────────────────────────────────────────────────┐ │
│   │ windy-search-service                                     │ │
│   │  • Verifies EPT JWT (Eternitas JWKS)                     │ │
│   │  • Checks EII score → applies rate limit                 │ │
│   │  • Routes to backend: Brave Search API / Browserbase /   │ │
│   │    Mozilla Readability / Claude vision extraction        │ │
│   │  • Logs result + outcome to Eternitas integrity events   │ │
│   │  • Returns clean JSON to agent                           │ │
│   └──────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## Endpoints (v1 spec — see `spec/agent-search-protocol.md`)

| Endpoint | Purpose | Backend |
|---|---|---|
| `POST /v1/web/search` | Search the web; returns top-N URLs + snippets | Brave Search API or SerpAPI |
| `POST /v1/web/fetch` | Download a URL; return readable text | fetch + Mozilla Readability |
| `POST /v1/web/browse` | Drive a real browser session — clicks, forms, screenshots | Browserbase or self-hosted Playwright |
| `POST /v1/web/extract` | LLM-driven structured extraction | Claude vision on screenshot |
| `POST /v1/web/research` | Higher-order: search + fetch top-N + synthesize | Composes the above |

## Ecosystem position

Windy Search is product #10 in the Windy family:

1. Windy Word — voice-to-text core
2. Windy Chat — unified comms hub
3. Windy Mail — email
4. Windy Cloud — storage
5. Windy Clone — voice clone marketplace
6. Windy Code — agent's operating environment (VS Code soft fork)
7. Windy Fly — the agents themselves
8. Windy Translate — translation models + API + marketplace
9. Windy Traveler — consumer travel companion
10. **Windy Search — agent web access** (this repo)
11. Eternitas — identity + trust registry (third-party / shared)

## Status

**The Phase 1 toolkit service is live in production** — `windy-search-service` v0.1.0,
deployed at `api.windysearch.com` (co-located on the consolidated EC2; see `SUBSTRATE.md`).
Milestones M0–M2 have shipped.

- ✅ Domain registered (windysearch.com on Cloudflare)
- ✅ Repo + scaffolding + canonical-domains lint vendored
- ✅ **M0 — service baseline live**: FastAPI service, EPT JWT verification via Eternitas
  JWKS, EII-aware rate limits, `/health`, `/health/ready`, `/whoami`, `/integrity`, and a
  `/version` endpoint (MF1 deployment identity)
- ✅ **M1 — search pipeline**: source router with fan-out, dedup + cap, ranking, and
  privacy normalization behind the canonical `POST /v1/search` endpoint
- ✅ **M2 — real backends**: Brave Search + Google adapters built + wired (they go live once
  their API keys are provisioned in prod), per-passport cost caps, and result caching
- ✅ Eternitas event-ingestion integration (`POST /api/v1/integrity/events`)
- ✅ Web toolkit endpoints: `POST /web/search`, `POST /web/fetch` (SSRF-hardened, Mozilla
  Readability), `POST /web/extract` (LLM-driven structured extraction)
- ⏳ `POST /v1/browse` and `POST /v1/research` (higher-order composition)
- ⏳ Phase 2 Chrome extension
- ⏳ Phase 3 standalone browser

> Note: the canonical agent-facing search endpoint shipped as `POST /v1/search`. The
> `/web/*` paths in the architecture/endpoint tables above reflect the original v1 spec and
> remain the implemented toolkit surface for fetch/extract.

## License

TBD. Service code likely Windy proprietary; spec + protocol open (intent: third-party platforms can adopt).
