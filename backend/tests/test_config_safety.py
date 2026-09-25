"""AAD-QUAL-006, AAD-OPS-007, AAD-OPS-008, AAD-OPS-009 — settings validated
at construction time, and `assert_deploy_safe` (formerly `assert_production_
safe`) actually protecting staging, not just production.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

# A fully "safe" baseline so each test below only has to override the one
# field it's actually exercising.
_SAFE_KWARGS = {
    "jwt_secret": "a" * 32,
    "payment_provider": "razorpay",
    "razorpay_key_id": "rzp_live_fake",
    "razorpay_key_secret": "a-real-looking-secret",
    "razorpay_webhook_secret": "a-real-looking-webhook-secret",
    "razorpay_callback_url": (
        "https://aadhya-farmfresh-production.up.railway.app/v1/payments/link-redirect"
    ),
    "database_url": "postgresql://user:pass@prod-db.internal:5432/aadhya",
    "google_web_client_id": "web-client-id",
    "google_android_client_id": "android-client-id",
    "cors_origins": "https://app.aadhyafarmfresh.com",
}


class TestLogLevelValidation:
    """AAD-QUAL-006: a bad LOG_LEVEL fails at Settings() construction, with a
    message naming the valid options — not later, inside configure_logging,
    as an unhelpful `ValueError: Unknown level`."""

    def test_a_valid_level_is_accepted_and_uppercased(self):
        assert Settings(log_level="debug").log_level == "DEBUG"
        assert Settings(log_level="WARNING").log_level == "WARNING"

    def test_an_invalid_level_is_rejected_at_construction(self):
        with pytest.raises(ValidationError, match="LOG_LEVEL must be one of"):
            Settings(log_level="verbose")

    def test_the_default_is_valid(self):
        assert Settings().log_level == "INFO"


class TestDeploySafeRunsForStagingToo:
    """AAD-OPS-007: staging used to skip every one of these checks."""

    def test_local_and_test_are_unaffected(self):
        Settings(env="local", jwt_secret="short").assert_deploy_safe()
        Settings(env="test", jwt_secret="short").assert_deploy_safe()

    def test_a_fully_safe_staging_config_passes(self):
        Settings(env="staging", **_SAFE_KWARGS).assert_deploy_safe()

    def test_a_fully_safe_production_config_passes(self):
        Settings(env="production", **_SAFE_KWARGS).assert_deploy_safe()

    def test_a_weak_jwt_secret_is_rejected_in_staging_not_just_production(self):
        kwargs = {**_SAFE_KWARGS, "jwt_secret": "too-short"}
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            Settings(env="staging", **kwargs).assert_deploy_safe()

    def test_a_wildcard_cors_origin_is_rejected_in_staging(self):
        kwargs = {**_SAFE_KWARGS, "cors_origins": "*"}
        with pytest.raises(RuntimeError, match="CORS_ORIGINS must not contain a wildcard"):
            Settings(env="staging", **kwargs).assert_deploy_safe()

    def test_the_documented_exception_mock_payments_are_allowed_in_staging(self):
        kwargs = {**_SAFE_KWARGS, "payment_provider": "mock"}
        Settings(env="staging", **kwargs).assert_deploy_safe()

    def test_the_same_exception_does_not_extend_to_production(self):
        kwargs = {**_SAFE_KWARGS, "payment_provider": "mock"}
        with pytest.raises(RuntimeError, match="PAYMENT_PROVIDER must not be 'mock'"):
            Settings(env="production", **kwargs).assert_deploy_safe()

    def test_assert_production_safe_is_still_callable_as_an_alias(self):
        """Nothing outside this module needs to know the method was renamed."""
        Settings(env="staging", **_SAFE_KWARGS).assert_production_safe()


class TestDeploySafeChecksTheThreeOmittedGaps:
    """AAD-OPS-008: DATABASE_URL still the localhost default; empty Google
    client IDs; a localhost CORS origin — none of these used to be checked
    at all, in any environment."""

    def test_database_url_still_at_its_localhost_default_is_rejected(self):
        kwargs = {**_SAFE_KWARGS, "database_url": Settings.model_fields["database_url"].default}
        with pytest.raises(RuntimeError, match="DATABASE_URL must not be left at its localhost"):
            Settings(env="production", **kwargs).assert_deploy_safe()

    def test_a_real_database_url_is_fine(self):
        Settings(env="production", **_SAFE_KWARGS).assert_deploy_safe()

    def test_an_empty_google_web_client_id_is_rejected(self):
        kwargs = {**_SAFE_KWARGS, "google_web_client_id": ""}
        with pytest.raises(RuntimeError, match="GOOGLE_WEB_CLIENT_ID"):
            Settings(env="production", **kwargs).assert_deploy_safe()

    def test_an_empty_google_android_client_id_is_rejected(self):
        kwargs = {**_SAFE_KWARGS, "google_android_client_id": ""}
        with pytest.raises(RuntimeError, match="GOOGLE_ANDROID_CLIENT_ID"):
            Settings(env="production", **kwargs).assert_deploy_safe()

    def test_a_localhost_cors_origin_is_rejected(self):
        kwargs = {**_SAFE_KWARGS, "cors_origins": "http://localhost:19006"}
        with pytest.raises(RuntimeError, match="CORS_ORIGINS must not contain a localhost origin"):
            Settings(env="production", **kwargs).assert_deploy_safe()

    def test_a_loopback_ip_cors_origin_is_also_rejected(self):
        kwargs = {**_SAFE_KWARGS, "cors_origins": "http://127.0.0.1:19006"}
        with pytest.raises(RuntimeError, match="CORS_ORIGINS must not contain a localhost origin"):
            Settings(env="production", **kwargs).assert_deploy_safe()


class TestRazorpayCallbackUrlHasNoDefault:
    """AAD-OPS-009: used to default to the production Railway URL, so a
    staging deploy that forgot to set it silently redirected paying
    customers into production after checkout."""

    def test_the_default_is_empty_not_a_production_url(self):
        assert Settings().razorpay_callback_url == ""

    def test_mock_provider_never_needs_it(self):
        """The default payment_provider is mock, which never reads this
        field — an empty default is safe precisely because of that."""
        Settings(env="production", **{**_SAFE_KWARGS, "payment_provider": "mock"})

    def test_razorpay_without_a_callback_url_is_rejected_in_staging(self):
        kwargs = {**_SAFE_KWARGS, "razorpay_callback_url": ""}
        with pytest.raises(RuntimeError, match="RAZORPAY_CALLBACK_URL is required"):
            Settings(env="staging", **kwargs).assert_deploy_safe()

    def test_razorpay_without_a_callback_url_is_rejected_in_production(self):
        kwargs = {**_SAFE_KWARGS, "razorpay_callback_url": ""}
        with pytest.raises(RuntimeError, match="RAZORPAY_CALLBACK_URL is required"):
            Settings(env="production", **kwargs).assert_deploy_safe()

    def test_constructing_a_real_razorpay_provider_without_it_also_fails(self, monkeypatch):
        """Defence in depth: the same gap is caught even outside
        staging/production, the moment anyone actually tries to build a
        RazorpayProvider with it unset — e.g. a developer testing against a
        real Razorpay sandbox locally."""
        from app.core.config import settings as live_settings
        from app.payments.razorpay import RazorpayProvider

        monkeypatch.setattr(live_settings, "razorpay_key_id", "rzp_test_fake")
        monkeypatch.setattr(live_settings, "razorpay_key_secret", "fake-secret")
        monkeypatch.setattr(live_settings, "razorpay_webhook_secret", "fake-webhook-secret")
        monkeypatch.setattr(live_settings, "razorpay_callback_url", "")

        with pytest.raises(RuntimeError, match="RAZORPAY_CALLBACK_URL is not configured"):
            RazorpayProvider()
