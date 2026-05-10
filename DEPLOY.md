# Windy Search — Production Deployment Guide

This document covers the EC2 deployment pattern for `api.windysearch.com`. It targets the M0 / M1 era of the master plan: a single-node FastAPI service + Redis behind Caddy. Postgres + own-corpus index land in M3 and will get an updated section then.

> **Companion to:** [windy-search master plan §6 M0.9](../kit-army-config/docs/windy-search-master-plan-2026-05-10.md), [ADR-014](../kit-army-config/docs/adr-014-windy-search-architecture.md).

---

## 1. Target infrastructure

| Component | Target |
|---|---|
| Compute | AWS EC2 — small (single node for M0/M1; scale to ALB+ASG once V1 ships) |
| Reverse proxy / TLS | Caddy 2 (auto-HTTPS via Let's Encrypt) |
| App container | `service/Dockerfile` runtime stage on port `:8500` |
| Cache / rate limit | Redis 7 (containerized via `docker-compose.yml`) |
| Object storage (M3+) | S3 bucket `windysearch-corpus` (WARC archive + Iceberg analytics) |
| Secrets | AWS SSM Parameter Store `/windy-search/prod/*` (loaded into `.env.production`) |
| DNS | Cloudflare zone `windysearch.com` — `A api.windysearch.com` → EC2 IP |

The container image is built from `service/` (not the repo root) — that's where `pyproject.toml` and `app/` live. The repo's `.github/`, `README.md`, and `spec/` do not ship in the wheel.

---

## 2. Repo layout on the host

The deploy directory on the EC2 host is **untracked** (matches the `windy-pro` and `eternitas` pattern per memory `feedback_windy_pro_deploy_layout.md`):

```
/opt/windy-search/
├─ deploy-prod/                  # untracked; pull from this repo periodically
│  ├─ docker-compose.yml         # copy of service/docker-compose.yml
│  ├─ Caddyfile                  # reverse-proxy config (see §4)
│  ├─ .env.production            # SECRETS — never committed
│  └─ logs/                      # docker volume mount
└─ src/                          # `git clone https://github.com/sneakyfree/windy-search`
```

The `deploy-prod/` directory is hand-curated. The `src/` checkout is the canonical Git working tree; deploys pull there, then copy the relevant files into `deploy-prod/`.

---

## 3. `.env.production`

Required keys (per `service/.env.example`, with prod values):

```sh
ENVIRONMENT=production
SERVICE_NAME=windy-search
LOG_LEVEL=INFO

# Redis (in-cluster Docker network)
REDIS_URL=redis://search-redis:6379/0

# Eternitas — the platform key + webhook secret are minted in eternitas
# admin once the platform is registered (see eternitas DEPLOY.md §6).
ETERNITAS_BASE_URL=https://api.eternitas.ai
ETERNITAS_JWKS_URL=https://api.eternitas.ai/.well-known/eternitas-keys
ETERNITAS_PLATFORM_API_KEY=et_plt_REDACTED
ETERNITAS_WEBHOOK_SECRET=REDACTED

# Search bridges (M2+)
BRAVE_SEARCH_API_KEY=REDACTED        # falls back to DDG when unset

# /web/extract — Anthropic OAuth (Grant's Max sub, dogfood only)
ANTHROPIC_OAUTH_TOKEN=sk-ant-oat01-REDACTED
ANTHROPIC_MODEL=claude-sonnet-4-6    # dev/test default per memory feedback_dev_prod_model_default

# Cost cap — base $5/mo, scaled by EI tier
MONTHLY_COST_CAP_USD_DEFAULT=5.0
MONTHLY_COST_WARNING_PCT=0.80
```

Per memory `reference_lockbox.md`, all REDACTED values live in `~/kit-army-config/secrets/`. Pull from there into the EC2 host via SSM Parameter Store, never via git.

Per memory `feedback_pydantic_settings_list_env.md`, do NOT override `CORS_ORIGINS` via env — `pydantic-settings` JSON-decodes list-typed env vars and a comma-separated value crashes boot. The default list in `app/config.py` is correct for prod; leave it alone.

---

## 4. Caddyfile

The host runs Caddy outside the container stack (binding 80/443 directly). `/opt/windy-search/deploy-prod/Caddyfile`:

```caddy
api.windysearch.com {
    reverse_proxy 127.0.0.1:8500
    encode gzip zstd
    log {
        output file /var/log/caddy/api.windysearch.com.log
        format json
    }
}
```

Per memory `feedback_caddy_inode_binding.md`, do NOT `cp` the Caddyfile (breaks bind mounts). Use `tee` or a text editor in place. Both the host file and any container-binding paths must match — capture both before edits.

---

## 5. Initial deploy

From a fresh EC2 host:

```sh
# 1. Clone canonical source
sudo mkdir -p /opt/windy-search && sudo chown ec2-user:ec2-user /opt/windy-search
cd /opt/windy-search
git clone https://github.com/sneakyfree/windy-search.git src

# 2. Provision deploy-prod/ from the source tree + secrets
mkdir -p deploy-prod logs
cp src/service/docker-compose.yml deploy-prod/docker-compose.yml
# ...edit deploy-prod/.env.production with values from kit-army-config/secrets/

# 3. Caddy (assumes apt-installed)
sudo cp deploy-prod/Caddyfile /etc/caddy/Caddyfile  # or tee per §4
sudo systemctl reload caddy

# 4. First container build + boot
cd deploy-prod
docker compose --env-file .env.production up -d --build

# 5. Verify
curl -fsS https://api.windysearch.com/health
# Expect: {"status":"ok","service":"windy-search","version":"0.1.0","environment":"production"}
```

---

## 6. Rolling deploy (subsequent updates)

Per memory `feedback_compose_restart_envfile.md`, `docker compose restart` reuses the existing container env block — new `.env` keys do NOT propagate. Always use `up -d --force-recreate` for env-changing deploys.

```sh
cd /opt/windy-search/src && git pull origin main

# Sync compose file if it changed
cp service/docker-compose.yml ../deploy-prod/docker-compose.yml

cd ../deploy-prod
docker compose --env-file .env.production up -d --build --force-recreate

# Verify
curl -fsS https://api.windysearch.com/health
docker compose --env-file .env.production logs --tail=50 search-api
```

When DB migrations land (M3+), chain `alembic upgrade head` per memory `feedback_manual_deploy_alembic.md`:

```sh
docker compose --env-file .env.production up -d --build --force-recreate
docker compose exec search-api alembic upgrade head
```

---

## 7. Smoke tests after deploy

```sh
# 1. Liveness
curl -fsS https://api.windysearch.com/health
# {"status":"ok",...}

# 2. Readiness (Redis reachable)
curl -fsS https://api.windysearch.com/health/ready
# {"status":"ready","redis":true}

# 3. Auth check (no EPT)
curl -i https://api.windysearch.com/whoami
# HTTP/2 401 — Bearer EPT required in Authorization header

# 4. Auth check (with valid EPT — pull from kit-army-config/secrets/test-ept.txt)
curl -i -H "Authorization: Bearer ${TEST_EPT}" https://api.windysearch.com/whoami
# HTTP/2 200 — passport claims JSON

# 5. EI tier + rate-limit budget
curl -i -H "Authorization: Bearer ${TEST_EPT}" https://api.windysearch.com/integrity
# HTTP/2 200 — {"passport":...,"score":...,"tier":"...","limit_per_minute":...}
```

If any of 1–3 fail, **roll back** before debugging in prod (see §8).

---

## 8. Rollback

```sh
cd /opt/windy-search/src && git log --oneline -5  # find prior known-good SHA
git checkout <prior-sha>

cd /opt/windy-search/deploy-prod
docker compose --env-file .env.production up -d --build --force-recreate

curl -fsS https://api.windysearch.com/health
```

After rollback works, return to `src/` and `git checkout main` (the rolled-back image stays running until the next forward-deploy).

---

## 9. Common operations

| Task | Command |
|---|---|
| Tail logs | `docker compose --env-file .env.production logs -f search-api` |
| Restart (env unchanged) | `docker compose --env-file .env.production restart search-api` |
| Restart (env changed) | `docker compose --env-file .env.production up -d --force-recreate search-api` |
| Inspect container env | `docker compose exec search-api env \| grep -i eternitas` |
| Redis inspection | `docker compose exec search-redis redis-cli ping` |
| Wipe rate-limit / cost-cap state | `docker compose exec search-redis redis-cli FLUSHDB` (DESTRUCTIVE) |
| Stop everything | `docker compose --env-file .env.production down` (preserves volumes) |
| Stop and wipe Redis state | `docker compose --env-file .env.production down -v` (DESTRUCTIVE) |

---

## 10. Pre-deploy checklist (run from the laptop)

```sh
# Canonical-domains lint must be green before deploy
bash .github/lint/lint-canonical-domains.sh \
  --config .github/lint/canonical-domains.json .

# Tests must pass
cd service && uv run pytest

# Lint must be clean
uv run ruff check app/ tests/

# Image must build
docker build -t windy-search-api:dryrun ./service
```

CI runs all four on every PR (`.github/workflows/ci.yml`). Don't deploy from a branch that hasn't passed CI.

---

## 11. Future sections (placeholder)

These land in the relevant phase:

- **§12 (M3)** — own-corpus deploy: Postgres + Quickwit/Manticore/Vespa selection + WARC ingestion job topology.
- **§13 (M4)** — JS-aware crawler: separate fleet of Browsertrix/Playwright workers + bandwidth budget guard + per-domain throttle.
- **§14 (M5)** — Eternitas-EPT enforcement at scale: JWKS rotation drills, key-compromise runbook.
- **§15 (M8)** — public launch: capacity planning, SLA monitoring, on-call rotation.

---

**End of M0 deployment guide.** Update this file with each milestone's deploy delta — don't let it drift behind the production state.
