"""Application configuration, loaded from environment / .env via pydantic-settings.

Phase 0: SQLite (aiosqlite) is the default dev database; DATABASE_URL flips to
Postgres later with no code change. Redis/Arq and real LLM/Nango wiring are Phase 1 —
their settings are declared here so the switch is config-only.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────
    app_env: str = "development"
    port: int = 4000
    document_storage_root: str = "./storage"

    # ── Database ─────────────────────────────────────
    # SQLite (async) by default. Postgres example:
    #   postgresql+asyncpg://user:pass@localhost:5432/insurance_os
    database_url: str = "sqlite+aiosqlite:///./insurance_os.db"

    # ── Redis / jobs (Arq) — Phase 1 ─────────────────
    redis_url: str = "redis://localhost:6379"

    # ── Auth (stub, Phase 0) ─────────────────────────
    # Premium ceiling a junior may approve; above this → escalate.
    junior_premium_cap: float = 150_000

    # ── LLM (OpenAI) — Phase 1 ───────────────────────
    openai_api_key: str = ""
    llm_model_fast: str = "gpt-4o-mini"
    llm_model_standard: str = "gpt-4o"
    llm_model_deep: str = "gpt-4.1"

    # ── Nango connectors — Phase 1 ───────────────────
    nango_secret_key: str = ""
    nango_host: str = "https://api.nango.dev"
    nango_integration_mail: str = "google-mail"
    nango_integration_sheet: str = "google-sheet"
    nango_integration_drive: str = "google-drive"
    connectors_mode: str = "mock"  # mock = fixtures offline | live = real Nango

    # ── Test data / fixtures ─────────────────────────
    test_data_root: str = ""

    # ── Quote Comparison recommendation weighting (QC-04/FR-18) ─────
    # Default price_weight=1.0/subjectivity_penalty=0.0 reproduces the
    # original pure-premium ranking exactly — configurable, not hardcoded.
    quote_rank_price_weight: float = 1.0
    quote_rank_subjectivity_penalty: float = 0.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
