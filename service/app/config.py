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

    # --- Search backends (B.4) -------------------------------------------
    brave_search_api_key: str | None = None  # falls back to DDG when None

    # --- B.6 Browserbase (deferred to its codon) -------------------------
    browserbase_api_key: str | None = None
    browserbase_project_id: str | None = None

    # --- B.7 Anthropic (Claude vision for extract; deferred) -------------
    anthropic_api_key: str | None = None

    # --- B.9 Cost caps ---------------------------------------------------
    monthly_cost_cap_usd_default: float = 5.0
    monthly_cost_warning_pct: float = 0.80

    # --- CORS ------------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: [
        "https://windysearch.com",
        "https://www.windysearch.com",
        "https://app.windyword.ai",
        "http://localhost:5173",
    ])


_cached_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide cached settings. Re-reads on test restart."""
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings
