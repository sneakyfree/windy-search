# SUBSTRATE — windy-search production

**ADR:** [ADR-048](https://github.com/sneakyfree/kit-army-config/blob/main/docs/adr-048-operational-substrate-as-code-2026-05-15.md) Layer 1
**Generated:** 2026-05-22 from `service/docker-compose.yml` (dev), `.github/workflows/deploy.yml`, repo config. Updated 2026-05-26 to reflect the committed `deploy-prod/docker-compose.yml` captured during the prod-compose-capture campaign — see Audit history.
**Maintenance policy:** edit on every change to compose, host directory layout, or env vars. Drift detector (ADR-048 Layer 2, T2.A — not yet shipped) will eventually verify this against the live host nightly.
**Confidence flags:** ⓘ = inferred-from-repo without live verification. ⚠ = known gap or pending action. ✓ = cross-verified against deploy.yml.

---

## Host

| Field | Value |
|---|---|
| EC2 instance ID | `i-07cef803a6a3f86b4` ✓ (consolidated EC2; per `deploy-prod/docker-compose.yml` header) |
| Public IPv4 | `54.88.113.79` ✓ (per same compose header; also tracked in lockbox + `DEPLOY_HOST` GHA secret) |
| SSH user | `ubuntu` ✓ (default for the consolidated box) |
| Repo path | `/opt/windy-search` ✓ (per deploy.yml `cd /opt/windy-search`) |
| Compose dir | `/opt/windy-search/deploy-prod` ✓ (per deploy.yml `cd deploy-prod`) |

windy-search runs **co-located on the consolidated EC2 `54.88.113.79`** alongside eternitas + windy-mail + windy-pro (per `deploy-prod/docker-compose.yml` header comment). The original "own EC2 per M0 baseline" plan was superseded; co-location is the v1 deploy posture and moving search to its own infra is a future operational decision.

## Compose project

| Field | Value |
|---|---|
| Project name | `windysearch-prod` ✓ (per committed `deploy-prod/docker-compose.yml` `name:` directive) |
| Dev compose | `service/docker-compose.yml` (in git; `name: windysearch-dev`) |
| Prod compose | `/opt/windy-search/deploy-prod/docker-compose.yml` ✓ **committed to git** as of 2026-05-26 (prod-compose-capture campaign closed the ADR-048 Layer 1 gap) |
| Env file | `/opt/windy-search/deploy-prod/.env.production` ✓ (per deploy.yml `--env-file .env.production`) |

The committed `deploy-prod/docker-compose.yml` matches the dev-compose shape; cold-start recoverability is now reproducible from git-state alone (modulo `.env.production` + volume data restore).

## Volumes — declared (dev) → on-host (inferred for prod)

| Compose name | On-host name | Critical data | Notes |
|---|---|---|---|
| `search-redis-data` | `deploy-prod_search-redis-data` ✓ (external; preserved across the 2026-05-20 project rename per Strategy A) | Redis appendonly: bridge cache, dedup, rate-limit counters | Re-buildable from upstream search bridges; loss causes brief cache miss spike |

No declared persistent app-data volume (windy-search holds no permanent state of its own — it's a stateless multi-bridge router per ADR-014 §12 cleanest leaf principle).

## Bind mounts

Unknown for prod (compose not in git). Dev compose has zero bind mounts. Inferred for prod:
- Possibly `/etc/caddy` or nginx for TLS termination at `api.windysearch.com`
- No persistent app-data dir expected

To be filled on next live audit.

## Services (running in prod)

| Compose service | Container name | Image | Healthy when |
|---|---|---|---|
| search-api | `windysearch-prod-search-api-1` ✓ | `windy-search-api:local` (built in-place from `service/Dockerfile`) | `curl http://localhost:8500/health` and `/version` (MF1) |
| search-redis | `windysearch-prod-search-redis-1` ✓ | `redis:7-alpine` (appendonly, 128M maxmem, allkeys-lru) | `redis-cli ping` |

## External ports (host-bound)

| Port | Service | Purpose |
|---|---|---|
| `127.0.0.1:8510->8500` | search-api (host 8510 → container 8500) ✓ | API loopback for Caddy proxy to `api.windysearch.com`. Caddy upstream targets host port 8510 directly. |

Redis (6379) is NOT host-bound — only reachable via the shared `deploy_backend` docker network.

## Network

- External shared bridge network `deploy_backend` ✓ (committed compose declares `networks.backend.external: true, name: deploy_backend`) — co-located with eternitas + windy-mail + windy-pro on the same network.
- Service-name prefix `search-` per compose header to avoid collisions on the shared network.

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

✓ **`deploy-prod/docker-compose.yml` is committed to git** as of 2026-05-26 (prod-compose-capture campaign). Cold-start is now reproducible from git-state alone (modulo `.env.production` + volume data).

✓ **EC2 ID + IP** are now recorded in the Host section (`i-07cef803a6a3f86b4` / `54.88.113.79`); the consolidated box is non-secret and surfaces in the committed compose header. Per-service identities + DEPLOY_HOST GHA secret still live in lockbox.

## Tolerated drift (allowlist)

Drift detector should NOT flag these:

| Item | Reason |
|---|---|
| Dev compose `name: windysearch-dev` vs prod `name: windysearch-prod` | Intentional — dev/prod project-name split prevents collisions on the consolidated EC2. |
| `:local` image tag (built in-place) | Sandbox-era pattern matching eternitas. Pin once external operators arrive. |
| Anonymous volume on `search-api` container (if any) | Stateless service; no persistent app-data declared. |

## Recovery — cold start from this manifest

Reproducible from git-state alone (with lockbox-restored `.env.production` and the external `deploy-prod_search-redis-data` volume preserved or rebuilt):

1. `git clone https://github.com/sneakyfree/windy-search /opt/windy-search`
2. Restore `/opt/windy-search/deploy-prod/.env.production` from lockbox.
3. Ensure the external `deploy-prod_search-redis-data` docker volume exists (created on first boot if absent — cache will repopulate from upstream bridges).
4. `cd /opt/windy-search/deploy-prod && sudo docker compose --env-file .env.production up -d`
5. Verify:
   - `curl https://api.windysearch.com/health` → `{"status":"healthy"}`
   - `curl https://api.windysearch.com/version` → MF1 metadata with deployed `commit_sha`
   - `curl https://api.windysearch.com/v1/search?q=test` → JSON response (bridge round-trip)

## Audit history

| Date | Trigger | Result |
|---|---|---|
| 2026-05-22 | Autonomous CTO loop T2.2 backfill | First substrate manifest authored from repo state. Cross-verified prod project name from deploy.yml. Live audit pending. |
| 2026-05-26 | Prod-compose-capture campaign (5 parallel SSH-verified captures) | `deploy-prod/docker-compose.yml` committed to git. Corrected EC2 co-location (consolidated `54.88.113.79`, NOT own EC2). Corrected port binding (`127.0.0.1:8510->8500`, not 8500). Corrected network (shared `deploy_backend`, not isolated `search-backend`). Promoted ⓘ→✓ on project name, container names, volume on-host name, and external network. |

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
