# Windy Search — repo notes for Claude

## What this repo is

Agent-centric web access for the Windy ecosystem. See `README.md` for the full vision; status today is "scaffolded, not yet built."

The thesis in one sentence: Windy Search is the toolkit a Windy Fly agent calls to interact with the open web, with Eternitas-passport-signed requests + Integrity-Index-aware rate limits + audit logs that feed the agent's score back into Eternitas.

## Branching policy

Same as the rest of the Windy ecosystem: feature branches + PR review. No direct commits to `main`. Squash-merge on land.

## Where things live

- `README.md` — vision + roadmap (read first)
- `spec/agent-search-protocol.md` — the HTTP wire protocol (in progress)
- `service/` — the v1 toolkit service (not yet started)
- `.github/lint/canonical-domains.{json,sh}` — vendored from kit-army-config; catches non-canonical domain references in PRs
- `.github/workflows/canonical-domains-lint.yml` — runs the lint on every push + PR

## Related repos in the ecosystem

- `windy-pro` — identity hub (account-server) that mints the EPT JWTs Windy Search verifies
- `eternitas` — trust registry; Windy Search reports score-affecting events here
- `windy-agent` (Windy Fly) — the agent runtime that calls Windy Search as a tool

## When working on this repo

- Read `kit-army-config/canonical-domains.json` before adding any external URL — the lint will catch you if you slip a banned hostname. (See the `banned[]` array in that file for the full list.)
- Eternitas Integrity Index events go to `POST /api/v1/integrity/events` — see eternitas repo's audit (memory `reference_eternitas_state_2026-05-06.md` if it exists)
- The v1 service should be designed as a stateless HTTP service that any Windy product or third-party agent platform can call. Don't bake in tight coupling to any one Windy product.

## Pre-launch checklist (when v1 is close)

- [ ] Ensure all endpoints verify EPT JWT signatures via Eternitas JWKS
- [ ] Per-EII rate limit policy implemented + tunable per environment
- [ ] Audit log → Eternitas integrity event ingest
- [ ] Cost monitoring per Eternitas-passport (prevent runaway spend)
- [ ] Backend abstraction so we can swap Brave Search / SerpAPI / Browserbase without touching agent-facing API
- [ ] Documented public spec at `windysearch.com/spec` for third-party adoption
