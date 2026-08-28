"""Typed application settings.

Everything the app needs to run is declared and validated at import time, so a
misconfigured deployment fails on boot rather than on the first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Runtime
    env: Environment = "local"
    log_level: str = "INFO"
    port: int = 8000

    # Database
    # Accepts the standard postgres:// or postgresql:// URL that Railway,
    # Supabase, Neon and RDS all hand out; normalised to the asyncpg driver
    # below so you can paste the provider's string verbatim.
    database_url: str = "postgresql://postgres:postgres@localhost:5432/aadhya"
    sql_echo: bool = False

    # Auth
    jwt_secret: str = "change-me-in-every-environment"
    jwt_algorithm: str = "HS256"
    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 60
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 45
    # How long a just-used correct code stays valid for an identical retry —
    # covers the client timing out after the server already succeeded.
    otp_consumed_grace_seconds: int = 120
    otp_debug_echo: bool = False

    # Payments
    payment_provider: Literal["mock", "razorpay"] = "mock"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    # The app's own deep-link URL that Razorpay redirects the browser back to
    # once a Payment Link is paid. Matches the scheme in mobile/app.config.js.
    razorpay_callback_url: str = "aadhya://payment-callback"

    # Store rules — all money is in the smallest currency unit (paise).
    currency: str = "INR"
    delivery_fee_paise: int = 2900
    free_delivery_threshold_paise: int = 29900
    min_order_paise: int = 0  # no minimum order; free-delivery threshold still applies

    # CORS
    cors_origins: str = "http://localhost:8081,http://localhost:19006"

    @property
    def async_database_url(self) -> str:
        """Force the asyncpg driver regardless of how the URL was supplied."""
        url = self.database_url
        for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
            if url.startswith(prefix):
                return url.replace("postgres+asyncpg://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic migrations, which run outside the event loop."""
        url = self.async_database_url
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def assert_production_safe(self) -> None:
        """Fail fast on configuration that is fine locally but unsafe in prod."""
        if not self.is_production:
            return
        problems: list[str] = []
        if self.jwt_secret == "change-me-in-every-environment" or len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be unique and at least 32 characters")
        if self.otp_debug_echo:
            problems.append("OTP_DEBUG_ECHO must be false")
        if self.payment_provider == "mock":
            problems.append("PAYMENT_PROVIDER must not be 'mock'")
        if self.payment_provider == "razorpay" and not (
            self.razorpay_key_id and self.razorpay_key_secret and self.razorpay_webhook_secret
        ):
            problems.append("Razorpay key id, secret and webhook secret are all required")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must not contain a wildcard")
        if problems:
            raise RuntimeError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.assert_production_safe()
    return s


settings = get_settings()
