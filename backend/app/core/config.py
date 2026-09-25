"""Typed application settings.

Everything the app needs to run is declared and validated at import time, so a
misconfigured deployment fails on boot rather than on the first request.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]

# AAD-QUAL-006: the level names logging.setLevel actually accepts. Checked
# here, at settings-construction time, so a typo like `LOG_LEVEL=verbose`
# fails fast with a message that says exactly what's wrong, instead of
# surfacing later as `ValueError: Unknown level: 'VERBOSE'` from deep inside
# `configure_logging()` — by which point, per AAD-QUAL-007 below, it's too
# late for the failure itself to even be logged in a structured way.
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _default_app_version() -> str:
    """AAD-QUAL-011: prefer whatever build identifier the deploy actually has.

    Railway injects `RAILWAY_GIT_COMMIT_SHA` into every deployment's
    environment automatically — no Dockerfile or CI change needed to pick it
    up. `GIT_SHA` is a generic fallback for any other CI system that sets one
    explicitly, and `APP_VERSION` (read like any other setting, so it's not
    duplicated here) lets a developer override either for a local test.
    `"dev"` is what's left for a plain local run with none of the above,
    which is honest about not being a traceable build.
    """
    for var in ("RAILWAY_GIT_COMMIT_SHA", "GIT_SHA"):
        value = os.environ.get(var)
        if value:
            return value[:12]
    return "dev"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Runtime
    env: Environment = "local"
    log_level: str = "INFO"
    port: int = 8000
    app_version: str = Field(default_factory=_default_app_version)

    # Database
    # Accepts the standard postgres:// or postgresql:// URL that Railway,
    # Supabase, Neon and RDS all hand out; normalised to the asyncpg driver
    # below so you can paste the provider's string verbatim.
    database_url: str = "postgresql://postgres:postgres@localhost:5432/aadhya"
    sql_echo: bool = False

    # AAD-PERF-002: all five were previously hardcoded in db/base.py, so
    # tuning per environment meant editing code. Pool settings default to
    # exactly what was hardcoded before (unchanged behaviour unless you set
    # an env var); the three timeouts are new — none existed at all, so a
    # single pathological query or lock wait could hold a connection (and,
    # with enough of them, the whole pool) indefinitely.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout_s: int = 30
    db_statement_timeout_ms: int = 10_000
    db_lock_timeout_ms: int = 3_000
    db_idle_in_transaction_timeout_ms: int = 15_000

    # Auth
    # AAD-SEC-008: SecretStr, not str — pydantic's default __repr__ dumps
    # every field verbatim, so anything that renders the settings object (a
    # debugger, an errant `log.info("config: %s", settings)`, an exception
    # whose frame locals get captured by an error reporter) used to write
    # this signing key to that destination in plaintext. SecretStr renders
    # as `**********` everywhere except `.get_secret_value()`.
    jwt_secret: SecretStr = SecretStr("change-me-in-every-environment")
    jwt_algorithm: str = "HS256"
    access_token_ttl_min: int = 30
    # AAD-SEC-002: cut from 60 to 30 days, and — unlike before — this is now
    # a sliding window rather than a fixed one: every refresh rotates in a
    # fresh 30-day token (see AuthService.refresh), so a session that keeps
    # being used never actually hits this ceiling; one that goes quiet does.
    refresh_token_ttl_days: int = 30

    # Payments
    payment_provider: Literal["mock", "razorpay"] = "mock"
    # Google Sign-In. Both client IDs are accepted as valid token audiences —
    # the Android app and (if ever added) a web client each get their own ID
    # from Google, and a token minted for either is legitimate.
    google_web_client_id: str = ""
    google_android_client_id: str = ""

    razorpay_key_id: str = ""  # not a secret — the public half of the API key pair
    razorpay_key_secret: SecretStr = SecretStr("")  # AAD-SEC-008
    razorpay_webhook_secret: SecretStr = SecretStr("")  # AAD-SEC-008
    # The app's own deep-link URL that Razorpay redirects the browser back to
    # once a Payment Link is paid. Matches the scheme in mobile/app.config.js.
    # Must be a real https:// URL — Razorpay's Payment Links API rejects a
    # custom app scheme (aadhya://...) outright with "URL should be sent in
    # callback_url field", confirmed against the live API, not assumed.
    # This points at our own backend instead, which immediately 302-redirects
    # the browser into the app's actual aadhya://payment-callback scheme —
    # see the /payments/link-redirect route for that hop.
    #
    # AAD-OPS-009: no default — this used to default to the production
    # Railway URL, so a staging deployment that simply forgot to set it
    # redirected a customer's browser into production after paying, the same
    # class of "wrong environment, silently" mistake as AAD-MOB-003. Left
    # blank, `RazorpayProvider.__init__` (app/payments/razorpay.py) refuses
    # to start, and `assert_deploy_safe` below catches it even earlier, at
    # boot, for staging and production alike. Empty is safe to ship as the
    # default because it's never read at all under the default
    # `payment_provider="mock"`.
    razorpay_callback_url: str = ""

    # Store rules — all money is in the smallest currency unit (paise).
    currency: str = "INR"
    delivery_fee_paise: int = 2900
    free_delivery_threshold_paise: int = 29900
    min_order_paise: int = 0  # no minimum order; free-delivery threshold still applies

    # AAD-BIZ-002: cash on delivery used to have no abuse controls at all —
    # an account created in seconds via Google sign-in could place unlimited
    # high-value COD orders to arbitrary addresses, with nothing at stake if
    # the delivery was refused at the door. These two are reasonable
    # starting numbers for a single-city dairy operation, not values backed
    # by any actual refusal/chargeback data (there isn't any yet) — revisit
    # once there are a few weeks of real numbers to look at.
    cod_max_active_orders_per_user: int = 2
    cod_max_order_value_paise: int = 300_000  # ₹3,000
    # Off by default: this blocks every brand-new account from using COD at
    # all on its first order (COD unlocks once one of their orders has
    # actually been delivered), which is a real conversion/UX trade-off, not
    # just a number — see AAD-BIZ-002 in the audit for the question this is
    # waiting on. The mechanism is implemented and tested; flip this once
    # that's decided.
    cod_requires_prior_delivery: bool = False

    # CORS
    cors_origins: str = "http://localhost:8081,http://localhost:19006"

    # AAD-SEC-017: no TrustedHostMiddleware existed at all, so a forged Host
    # header would be trusted anywhere the app builds an absolute URL from
    # it (nothing does yet — but razorpay_callback_url is exactly the kind
    # of field that pattern starts with, and password-reset-style flows are
    # the classic victim once they exist). `aadhya-farmfresh-production.
    # up.railway.app` is the one real production host this codebase already
    # names (see razorpay_callback_url's default history above); `testserver`
    # and `test` are the two Host headers httpx's own ASGITransport clients
    # send in this test suite (`base_url="http://testserver"` in some
    # fixtures, the bare httpx default of `"http://test"` in others — both
    # pre-existing, both genuinely test-only, no real request ever carries
    # either), not a production exception.
    allowed_hosts: str = (
        "localhost,127.0.0.1,testserver,test,aadhya-farmfresh-production.up.railway.app"
    )

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
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        upper = v.upper()
        if upper not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {v!r}"
            )
        return upper

    def assert_deploy_safe(self) -> None:
        """Fail fast on configuration that is fine locally but unsafe once
        real customers or real money are involved.

        AAD-OPS-007: this used to return immediately unless `env ==
        "production"`, so staging got no safety net at all — a staging box
        that happens to carry live-looking secrets (someone testing the real
        Razorpay flow end-to-end before a release, say) passed with a weak
        JWT secret, a wildcard CORS origin, or a database URL still pointing
        at localhost, and nothing said so until it mattered. Every check
        below now runs for staging and production alike, with exactly one
        documented exception: `PAYMENT_PROVIDER=mock` is still allowed in
        staging (not production), because a staging environment used purely
        for QA against the mobile app — not the payment gateway itself — is
        a legitimate, common setup here, and forcing every staging box to
        hold sandbox Razorpay credentials just to boot is friction with no
        safety benefit. Everything else is identical between the two.
        """
        if self.env not in ("staging", "production"):
            return
        problems: list[str] = []
        # SecretStr defines __len__ (so `len(self.jwt_secret)` still works
        # directly) but not __eq__ against a plain str, so the
        # "still-the-default" comparison needs .get_secret_value().
        if (
            self.jwt_secret.get_secret_value() == "change-me-in-every-environment"
            or len(self.jwt_secret) < 32
        ):
            problems.append("JWT_SECRET must be unique and at least 32 characters")
        # AAD-OPS-007's one documented exception: mock payments stay allowed
        # in staging, never in production.
        if self.payment_provider == "mock" and self.is_production:
            problems.append("PAYMENT_PROVIDER must not be 'mock' in production")
        if self.payment_provider == "razorpay":
            if not (
                self.razorpay_key_id and self.razorpay_key_secret and self.razorpay_webhook_secret
            ):
                problems.append("Razorpay key id, secret and webhook secret are all required")
            # AAD-OPS-009: required explicitly once real payments are in
            # play, in either environment — an empty value here would 404 the
            # customer's browser instead of returning them to the app.
            if not self.razorpay_callback_url:
                problems.append("RAZORPAY_CALLBACK_URL is required when PAYMENT_PROVIDER=razorpay")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must not contain a wildcard")
        # AAD-OPS-008: the three specific gaps the audit called out by name —
        # each one fails *open* today (a deploy missing one of these doesn't
        # crash, it just quietly serves from the wrong place or refuses every
        # sign-in) rather than failing loudly at boot, which is the whole
        # point of this method.
        if self.database_url == Settings.model_fields["database_url"].default:
            problems.append("DATABASE_URL must not be left at its localhost default")
        if not self.google_web_client_id or not self.google_android_client_id:
            problems.append("GOOGLE_WEB_CLIENT_ID and GOOGLE_ANDROID_CLIENT_ID are both required")
        if any("localhost" in origin or "127.0.0.1" in origin for origin in self.cors_origin_list):
            problems.append("CORS_ORIGINS must not contain a localhost origin")
        # AAD-SEC-017: a wildcard here would make TrustedHostMiddleware a
        # no-op — the exact protection this setting exists to provide.
        if "*" in self.allowed_host_list:
            problems.append("ALLOWED_HOSTS must not contain a wildcard")
        _local_hosts = ("localhost", "127.0.0.1", "testserver", "test")
        if any(h in _local_hosts for h in self.allowed_host_list):
            problems.append("ALLOWED_HOSTS must not contain a local/test host")
        if problems:
            raise RuntimeError(
                f"Unsafe {self.env} configuration:\n  - " + "\n  - ".join(problems)
            )

    # AAD-OPS-007: kept as an alias — the old name read naturally when this
    # only ever ran for production; `assert_deploy_safe` is the one to reach
    # for now that staging runs the same checks.
    assert_production_safe = assert_deploy_safe


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.assert_deploy_safe()
    return s


settings = get_settings()
