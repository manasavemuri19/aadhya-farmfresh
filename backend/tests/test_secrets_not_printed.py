"""AAD-SEC-008 — jwt_secret, razorpay_key_secret and razorpay_webhook_secret
are `SecretStr`, so nothing that renders the settings object (a debugger, a
stray `log.info("config: %s", settings)`, an exception whose frame locals
get captured by an error reporter) writes the real value in plaintext —
while the actual signing/verification code paths still get the real string
via `.get_secret_value()` at the one point each needs it.
"""

from __future__ import annotations

import jwt as pyjwt
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.security import decode_token, issue_access_token


@pytest.fixture
def settings_with_recognisable_secrets():
    return Settings(
        jwt_secret="a-recognisable-jwt-secret-value-32chars",
        razorpay_key_secret="a-recognisable-razorpay-secret",
        razorpay_webhook_secret="a-recognisable-webhook-secret",
    )


def test_repr_does_not_contain_the_jwt_secret(settings_with_recognisable_secrets):
    assert "a-recognisable-jwt-secret-value-32chars" not in repr(settings_with_recognisable_secrets)


def test_str_does_not_contain_the_razorpay_secrets(settings_with_recognisable_secrets):
    rendered = str(settings_with_recognisable_secrets)
    assert "a-recognisable-razorpay-secret" not in rendered
    assert "a-recognisable-webhook-secret" not in rendered


def test_fields_are_secretstr_not_plain_str(settings_with_recognisable_secrets):
    assert isinstance(settings_with_recognisable_secrets.jwt_secret, SecretStr)
    assert isinstance(settings_with_recognisable_secrets.razorpay_key_secret, SecretStr)
    assert isinstance(settings_with_recognisable_secrets.razorpay_webhook_secret, SecretStr)


def test_get_secret_value_still_returns_the_real_string(settings_with_recognisable_secrets):
    assert (
        settings_with_recognisable_secrets.jwt_secret.get_secret_value()
        == "a-recognisable-jwt-secret-value-32chars"
    )


def test_token_issue_and_decode_still_round_trip_through_the_wrapped_secret(monkeypatch):
    """The point of SecretStr isn't just hiding the repr — security.py must
    still actually sign and verify tokens correctly with the real value."""
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "jwt_secret", SecretStr("a-different-recognisable-secret-at-least-32-chars")
    )

    token = issue_access_token("usr_test123", role="customer")
    payload = decode_token(token, expected_type="access")
    assert payload["sub"] == "usr_test123"

    # And it really was signed with the wrapped value, not some default —
    # decoding with the wrong secret must fail.
    with pytest.raises(pyjwt.InvalidTokenError):
        pyjwt.decode(token, "the-wrong-secret-entirely", algorithms=["HS256"])
