"""Google Sign-In.

Real JWKS-backed verification (`_decode_google_id_token`) talks to Google's
real servers to fetch signing keys, and there is no way to produce a
genuinely Google-signed token in a test without a live network call to
Google — so these tests mock that one function and prove everything around
it: audience checking, `email_verified` handling, first-time account
creation, returning-user login, and the failure paths, including the
network-vs-invalid-token distinction AAD-SEC-012 added. The one thing this
suite cannot prove is that PyJWT's own signature-verification code correctly
rejects a forged token — that part is the library's job, not this app's.
"""

from __future__ import annotations

from unittest.mock import patch

import jwt
import pytest

from app.core.errors import Unauthorized, UpstreamError
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.services.auth_service import AuthService


@pytest.fixture
def users(session) -> UserRepository:
    return UserRepository(session)


@pytest.fixture
def auth(session, users) -> AuthService:
    return AuthService(users, RefreshTokenRepository(session))


def _claims(**overrides) -> dict:
    base = {
        "sub": "google_sub_12345",
        "email": "customer@example.com",
        "email_verified": True,
        "name": "Test Customer",
        "aud": "test-web-client-id",
    }
    return {**base, **overrides}


@pytest.fixture(autouse=True)
def google_client_ids(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "google_web_client_id", "test-web-client-id")
    monkeypatch.setattr(settings, "google_android_client_id", "test-android-client-id")


async def test_first_time_sign_in_creates_an_account(auth, session):
    with patch("app.services.auth_service._decode_google_id_token", return_value=_claims()):
        tokens, profile = await auth.verify_google_and_login("fake-token")

    assert profile.email == "customer@example.com"
    assert profile.name == "Test Customer"
    assert profile.role == "customer"
    assert tokens.access_token


async def test_returning_user_logs_into_the_same_account(auth, session):
    with patch("app.services.auth_service._decode_google_id_token", return_value=_claims()):
        _, first = await auth.verify_google_and_login("fake-token-1")
        _, second = await auth.verify_google_and_login("fake-token-2")

    assert first.id == second.id


async def test_a_name_change_in_app_survives_re_login(auth, users, session):
    """Google's profile name must not silently overwrite one the customer
    has since edited in their own profile."""
    with patch("app.services.auth_service._decode_google_id_token", return_value=_claims()):
        _, profile = await auth.verify_google_and_login("fake-token")

    await users.update_profile(profile.id, {"name": "Edited In App"})
    await session.flush()

    with patch("app.services.auth_service._decode_google_id_token", return_value=_claims()):
        _, second = await auth.verify_google_and_login("fake-token-again")

    assert second.name == "Edited In App"


async def test_token_for_a_different_app_is_rejected(auth, session):
    """The audience check — this is what stops a token minted for some
    unrelated app from being replayed against this backend."""
    bad_claims = _claims(aud="some-other-apps-client-id")
    with patch("app.services.auth_service._decode_google_id_token", return_value=bad_claims):
        with pytest.raises(Unauthorized):
            await auth.verify_google_and_login("fake-token")


async def test_a_token_that_fails_googles_own_verification_is_rejected(auth, session):
    with patch(
        "app.services.auth_service._decode_google_id_token",
        side_effect=jwt.ExpiredSignatureError("Signature has expired"),
    ):
        with pytest.raises(Unauthorized):
            await auth.verify_google_and_login("garbage-token")


async def test_an_unresolvable_signing_key_is_rejected_as_unauthorized(auth, session):
    """A malformed or foreign token whose `kid` doesn't match anything in
    Google's JWKS — distinct from a network failure (below), and correctly
    still a 401: the token itself, not the network, is the problem."""
    with patch(
        "app.services.auth_service._decode_google_id_token",
        side_effect=jwt.PyJWKClientError("Unable to find a signing key"),
    ):
        with pytest.raises(Unauthorized):
            await auth.verify_google_and_login("garbage-token")


async def test_email_is_stored_only_when_google_reports_it_verified(auth, session):
    """AAD-SEC-011: an unverified email must never be persisted as if it
    were a confirmed fact about the account."""
    with patch(
        "app.services.auth_service._decode_google_id_token",
        return_value=_claims(email_verified=False),
    ):
        _, profile = await auth.verify_google_and_login("fake-token")

    assert profile.email is None


async def test_a_network_failure_reaching_google_is_reported_as_upstream_not_unauthorized(
    auth, session
):
    """AAD-SEC-012: this used to be indistinguishable from a bad token —
    reported to the user as "wrong credentials" and invisible in the error
    rate as a 401. It must surface as a distinct, retryable failure."""
    with patch(
        "app.services.auth_service._decode_google_id_token",
        side_effect=jwt.PyJWKClientConnectionError("Fail to fetch data from the url"),
    ):
        with pytest.raises(UpstreamError):
            await auth.verify_google_and_login("fake-token")


async def test_no_configured_client_ids_refuses_rather_than_silently_accepting(
    auth, session, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "google_web_client_id", "")
    monkeypatch.setattr(settings, "google_android_client_id", "")

    with pytest.raises(Unauthorized):
        await auth.verify_google_and_login("fake-token")


async def test_profile_update_is_visible_immediately_in_the_same_request(
    auth, users, session,
):
    """Regression test for a real bug: update_profile and upsert_address both
    mutate ORM objects in memory, and this session has autoflush off. A
    request that saves a profile and then re-reads it to build its response
    (exactly what /auth/me PATCH does) would otherwise see the database's
    pre-update state — the save would look like it silently discarded the
    address, even though it committed correctly at the end of the request.
    """
    with patch("app.services.auth_service._decode_google_id_token", return_value=_claims()):
        _, profile = await auth.verify_google_and_login("fake-token")

    await users.update_profile(profile.id, {"name": "Real Name", "phone": "9876543210"})
    await users.upsert_address(profile.id, {
        "label": "Home", "line1": "12-3-45 Banjara Hills", "line2": "",
        "landmark": "", "city": "Hyderabad", "pincode": "500034",
        "latitude": None, "longitude": None,
    })

    # Same session, no new transaction — this is what the route handler does.
    reread = await users.get_by_id(profile.id)

    assert reread["name"] == "Real Name"
    assert reread["phone"] == "9876543210"
    assert len(reread["addresses"]) == 1
    assert reread["addresses"][0]["line1"] == "12-3-45 Banjara Hills"


async def test_updating_the_same_field_twice_reflects_the_latest_value(auth, users, session):
    with patch("app.services.auth_service._decode_google_id_token", return_value=_claims()):
        _, profile = await auth.verify_google_and_login("fake-token")

    await users.update_profile(profile.id, {"name": "First"})
    await users.update_profile(profile.id, {"name": "Second"})

    reread = await users.get_by_id(profile.id)
    assert reread["name"] == "Second"
