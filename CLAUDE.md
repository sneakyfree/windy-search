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

## Windy Admin telemetry (stress-test contract) — 2026-07-07

**There is ONE super-admin dashboard for the whole ecosystem at
`https://admin.windyword.ai`** (repo `sneakyfree/windy-admin`, ADR-WA-001).
Every platform PUSHES small content-free JSON events to its central ingest;
the dashboard reads them (Fleet / Members / Agents / Cost / Experiment /
Audit). **When you stress-test this platform, it is part of the job to prove
its activity shows up on that dashboard.** Missing telemetry is a GAP to
close, exactly like a bug — and each platform verifying its own telemetry in
parallel doubles as a load test of the panel.

**This repo:** LIVE — search-api emits cost.charge / cost.denied / cost.refund at the cost_cap choke point. Code: `service/app/telemetry.py` + `service/app/eii/cost_cap.py`. Token `…__WINDY_SEARCH`.

**Verify during your stress test** (read token `verify-oc5` in lockbox
`secrets/windy-admin/ingest-tokens.env`):
```bash
curl -s "https://admin.windyword.ai/v1/events/tail?limit=10&platform=windy-search" \
  -H "Authorization: Bearer <verify-oc5>" | python3 -m json.tool
```
Drive real traffic, then confirm your events appear here AND on the dashboard
page they feed. If they don't, fix the emitter (or the aggregation query).

**Hard rules (non-negotiable):**
- **Fire-and-forget**: post async, short timeout, swallow every error. A dead
  ingest must NEVER break this product (proven: chat runs fine with the ingest
  down).
- **Inert unless configured**: no-op when `WINDY_ADMIN_INGEST_URL` /
  `WINDY_ADMIN_INGEST_TOKEN` are unset.
- **Privacy hard line**: counts / costs / durations / models / ids only. Cost
  is INTEGER microcents (10^-6 USD). The ingest 422s any metadata key whose
  camelCase/snake tokens match content/text/body/message/prompt/transcript/
  subject/html/completion/reply — if you get 422'd, FIX THE EVENT, never ask
  for the guard to be loosened.

**Full brief + per-platform table + how-to-instrument:**
`~/kit-army-config/docs/windy-admin-telemetry-campaign-2026-07-07.md`.

## CI: self-hosted runner (since 2026-07)
GitHub Actions runs on OUR runner (kit0-windy-search on the Kit 0 VPS), not GitHub's cloud.
Always `runs-on: [self-hosted, linux, x64]` — NEVER `ubuntu-latest` (billing-locked; runner-lint enforces).
Jobs stuck "Queued" = runner down, not billing: ssh Kit 0 → cd /home/github-runner/runners/windy-search && sudo ./svc.sh status
Full runbook: ~/kit-army-config/docs/ci-runner-runbook.md
