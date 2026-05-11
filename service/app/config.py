"""Pydantic-settings model for the Windy Search service."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-driven configuration for the service.

    The codon stages each capability behind its own optional env var so
    the service runs in degraded mode when a dependency isn't yet
    provisioned (e.g., no Brave key → DDG fallback only; no Eternitas
    creds → events buffered but not posted upstream).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service identity -------------------------------------------------
    environment: str = "development"  # development | staging | production
    service_name: str = "windy-search"
    log_level: str = "INFO"

    # --- HTTP server ------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8500

    # --- Redis (B.3 rate limit, B.10 cache, B.9 cost cap) -----------------
    redis_url: str | None = None  # e.g. "redis://localhost:6379/0"

    # --- Eternitas integration (B.2 auth, B.4-B.8 event emission) ---------
    eternitas_base_url: str = "https://api.eternitas.ai"
    eternitas_jwks_url: str = "https://api.eternitas.ai/.well-known/eternitas-keys"
    eternitas_platform_api_key: str | None = None  # et_plt_* — required to POST integrity events
    # The webhook_secret eternitas registered alongside our platform key.
    # When present, /webhooks verifies X-Eternitas-Signature HMAC and acts
    # on integrity.event payloads (invalidates the cached EII score).
    # When absent, /webhooks 204s without consumption.
    eternitas_webhook_secret: str | None = None

    # --- Search backends (B.4) -------------------------------------------
    brave_search_api_key: str | None = None  # falls back to DDG when None

    # Google Custom Search (M2.2) — LAST-RESORT bridge per master plan §4
    # P1. Requires BOTH a Custom Search JSON API key AND a Custom Search
    # Engine ID. When either is missing, the GoogleSource stays dormant
    # (is_configured()=False); the router skips it.
    google_search_api_key: str | None = None
    google_cse_id: str | None = None

    # --- B.6 Browserbase (deferred to its codon) -------------------------
    browserbase_api_key: str | None = None
    browserbase_project_id: str | None = None

    # --- B.7 Anthropic (Claude for structured extraction) ---------------
    # OAuth token (sk-ant-oat01-*) for Grant's $200/mo Max plan. Per
    # feedback_anthropic_oauth_gate.md, this requires Bearer auth +
    # `anthropic-beta: oauth-2025-04-20` + a two-block system array
    # whose first block is the magic Claude-Code gate string.
    # ToS reminder: only legitimate for personal use of Grant's own
    # subscription. Multi-user fan-out should migrate to per-user
    # sk-ant-api03-* keys or Bedrock (Bill's AWS account, see lockbox).
    anthropic_oauth_token: str | None = None
    anthropic_model: str = "claude-sonnet-4-6"

    # --- B.9 Cost caps ---------------------------------------------------
    monthly_cost_cap_usd_default: float = 5.0
    monthly_cost_warning_pct: float = 0.80

    # --- CORS ------------------------------------------------------------
    # Canonical hostnames per kit-army-config/canonical-domains.json v6.
    # The formerly-used Word `app.*` subdomain is now in `banned[]` —
    # canonical Word identity host is `account.windyword.ai`.
    # `windymind.ai` was added 2026-05-10 as the BYOM intelligence layer
    # (Platform 12) per ADR-010 §2.
    cors_origins: list[str] = Field(default_factory=lambda: [
        "https://windysearch.com",
        "https://www.windysearch.com",
        "https://account.windyword.ai",
        "https://windymind.ai",
        "http://localhost:5173",
    ])


_cached_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide cached settings. Re-reads on test restart."""
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings
