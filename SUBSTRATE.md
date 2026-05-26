# SUBSTRATE — windy-search production

**ADR:** [ADR-048](https://github.com/sneakyfree/kit-army-config/blob/main/docs/adr-048-operational-substrate-as-code-2026-05-15.md) Layer 1
**Generated:** 2026-05-22 from `service/docker-compose.yml` (dev), `.github/workflows/deploy.yml`, repo config. Mostly inferred-from-repo since the prod compose is hand-curated on the EC2 — same gap as windy-cloud, see Known Gaps.
**Maintenance policy:** edit on every change to compose, host directory layout, or env vars. Drift detector (ADR-048 Layer 2, T2.A — not yet shipped) will eventually verify this against the live host nightly.
**Confidence flags:** ⓘ = inferred-from-repo without live verification. ⚠ = known gap or pending action. ✓ = cross-verified against deploy.yml.

---

## Host

| Field | Value |
|---|---|
| EC2 instance ID | (in lockbox `ACCESS_LOCKBOX.md`) ⚠ |
| Public IPv4 | (in lockbox + as `DEPLOY_HOST` GHA secret) ⚠ |
| SSH user | `ubuntu` ⓘ (default for the triad services) |
| Repo path | `/opt/windy-search` ✓ (per deploy.yml `cd /opt/windy-search`) |
| Compose dir | `/opt/windy-search/deploy-prod` ✓ (per deploy.yml `cd deploy-prod`) |

windy-search runs on **its own EC2** (NOT co-located with eternitas/windy-mail/windy-pro). The triad services (search/call/text/cell) each get their own EC2 per the M0 baseline pattern.

## Compose project

| Field | Value |
|---|---|
| Project name | `windysearch-prod` ✓ (per deploy.yml verification container `windysearch-prod-search-api-1`) |
| Dev compose | `service/docker-compose.yml` (in git; `name: windysearch-dev`) |
| Prod compose | `/opt/windy-search/deploy-prod/docker-compose.yml` ⚠ **NOT in git** — hand-curated on EC2 only (same gap as windy-cloud, see Known Gaps) |
| Env file | `/opt/windy-search/deploy-prod/.env.production` ✓ (per deploy.yml `--env-file .env.production`) |

The dev compose at `service/docker-compose.yml` mirrors the prod shape per its comment block: *"Mirrors eternitas's deploy-prod/docker-compose.yml shape so the eventual prod compose file is a near-clone."* Confidence on prod-shape inference is therefore high.

## Volumes — declared (dev) → on-host (inferred for prod)

| Compose name | On-host name (inferred) | Critical data | Notes |
|---|---|---|---|
| `search-redis-data` | `windysearch-prod_search-redis-data` ⓘ | Redis appendonly: bridge cache, dedup, rate-limit counters | Re-buildable from upstream search bridges; loss causes brief cache miss spike |

No declared persistent app-data volume (windy-search holds no permanent state of its own — it's a stateless multi-bridge router per ADR-014 §12 cleanest leaf principle).

## Bind mounts

Unknown for prod (compose not in git). Dev compose has zero bind mounts. Inferred for prod:
- Possibly `/etc/caddy` or nginx for TLS termination at `api.windysearch.com`
- No persistent app-data dir expected

To be filled on next live audit.

## Services (expected running in prod)

| Compose service | Container name | Image | Healthy when |
|---|---|---|---|
| search-api | `windysearch-prod-search-api-1` ✓ | `windy-search-api:local` (built in-place from `service/Dockerfile`) | `curl http://localhost:8500/health` and `/version` (MF1) |
| search-redis | `windysearch-prod-search-redis-1` ⓘ | `redis:7-alpine` (appendonly, 128M maxmem, allkeys-lru) | `redis-cli ping` |

## External ports (host-bound)

| Port | Service | Purpose |
|---|---|---|
| `127.0.0.1:8500` | search-api → 8500 (container) | API loopback for Caddy proxy to `api.windysearch.com` |

Redis (6379) is NOT host-bound — only reachable via the `search-backend` docker network.

## Network

- Single internal bridge network `search-backend` (compose-managed; dev). Prod likely same shape.
- NO external network attachments (not on a shared `deploy_backend` like windy-mail/eternitas).

## Critical env vars (must be present in /opt/windy-search/deploy-prod/.env.production)

**Required for boot (per ADR-014 + the search M0 baseline):**
- `REDIS_URL` (or use the in-network `redis://search-redis:6379/0`)
- `ENVIRONMENT=production`

**Required for bridge providers** (provider-specific keys — see ADR-014 §portfolio):
- `BRAVE_API_KEY` (primary bridge)
- `GOOGLE_*` (fallback bridge, costs per query)
- Additional bridge keys per `service/app/bridges/`

**Required for upstream auth:**
- `WINDY_PRO_JWKS_URL` (humans authenticate via Pro JWKS RS256)
- `ETERNITAS_JWKS_URL` (agents authenticate via Eternitas EPT)

**MF1 deploy-identity (set by deploy workflow at build time):**
- `COMMIT_SHA`
- `BUILD_TIMESTAMP`
- `ENVIRONMENT=production`

Per `[[feedback_pydantic_settings_list_env]]`: `CORS_ORIGINS` must be JSON-quoted in `.env.production` for pydantic-settings list parsing.

## Known gaps + audit findings

⚠ **`deploy-prod/docker-compose.yml` is NOT committed to git.** Same gap as windy-cloud — the deploy workflow expects it to exist at `/opt/windy-search/deploy-prod/docker-compose.yml` on the EC2 only. Cold-start recovery from git alone is currently impossible.

**Grant-on-return action:** SSH to the windy-search EC2, capture `docker compose -f deploy-prod/docker-compose.yml config`, sanitize secrets, and commit to repo at `deploy-prod/docker-compose.yml`. This closes:
- Cold-start recoverability (substrate-as-code)
- The "windy-search has unaddressed auto-deploy gap" pattern parallel to windy-cloud and windy-mind

⚠ **EC2 ID + IP not in this manifest** — both live in `ACCESS_LOCKBOX.md` + as GHA secrets (`DEPLOY_HOST`). Per `[[feedback_no_secrets_in_public_docs]]`, kept by-reference not by-value.

## Tolerated drift (allowlist)

Drift detector should NOT flag these:

| Item | Reason |
|---|---|
| Dev compose `name: windysearch-dev` vs prod `name: windysearch-prod` | Intentional — prod compose lives only on host, see Known Gaps. |
| `:local` image tag (built in-place) | Sandbox-era pattern matching eternitas. Pin once external operators arrive. |
| Anonymous volume on `search-api` container (if any) | Stateless service; no persistent app-data declared. |

## Recovery — cold start from this manifest

Currently INCOMPLETE without `deploy-prod/docker-compose.yml`. Steps that work today:

1. `git clone https://github.com/sneakyfree/windy-search /opt/windy-search`
2. Recover `deploy-prod/docker-compose.yml` from lockbox or known-good EC2 snapshot.
3. Restore `/opt/windy-search/deploy-prod/.env.production` from lockbox.
4. `cd /opt/windy-search/deploy-prod && sudo docker compose --env-file .env.production up -d`
5. Verify:
   - `curl https://api.windysearch.com/health` → `{"status":"healthy"}`
   - `curl https://api.windysearch.com/version` → MF1 metadata with deployed `commit_sha`
   - `curl https://api.windysearch.com/v1/search?q=test` → JSON response (bridge round-trip)

## Audit history

| Date | Trigger | Result |
|---|---|---|
| 2026-05-22 | Autonomous CTO loop T2.2 backfill | First substrate manifest authored from repo state. Cross-verified prod project name from deploy.yml. Live audit pending. |

## Cross-references

- ADR-014: `kit-army-config/docs/adr-014-windy-search-architecture.md` (12 principles)
- ADR-048: `kit-army-config/docs/adr-048-operational-substrate-as-code-2026-05-15.md`
- windy-mail SUBSTRATE.md (reference impl): `/Users/thewindstorm/windy-mail/deploy/SUBSTRATE.md`
- windy-cloud SUBSTRATE.md (same prod-compose-gap pattern): `/Users/thewindstorm/windy-cloud/SUBSTRATE.md`
- eternitas SUBSTRATE.md (same triad-deploy pattern): `/Users/thewindstorm/eternitas/deploy-prod/SUBSTRATE.md`
- Memory: `project_windy_search_cleanest_leaf.md`
- Memory: `feedback_mind_auto_deploy_unwired.md` (notes that Cloud + Chat + others have similar gaps)
- Memory: `feedback_pydantic_settings_list_env.md`
- Memory: `reference_lockbox.md`
