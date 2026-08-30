"""Google Sign-In.

`google.oauth2.id_token.verify_oauth2_token` itself talks to Google's real
servers to fetch signing keys — there is no way to produce a genuinely
Google-signed token in a test without a live network call to Google, so
these tests mock that one function and prove everything around it: audience
checking, first-time account creation, returning-user login, and the
failure paths. The one thing this suite cannot prove is that Google's actual
signature-verification code correctly rejects a forged token — that part is
Google's own library's job, not this app's.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.errors import Unauthorized
from app.repositories.otp import OtpRepository
from app.repositories.users import UserRepository
from app.services.auth_service import AuthService


@pytest.fixture
def users(session) -> UserRepository:
    return UserRepository(session)


@pytest.fixture
def auth(session, users) -> AuthService:
    return AuthService(users, OtpRepository(session))


def _claims(**overrides) -> dict:
    base = {
        "sub": "google_sub_12345",
        "email": "customer@example.com",
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
    with patch("app.services.auth_service.google_id_token.verify_oauth2_token", return_value=_claims()):
        tokens, profile = await auth.verify_google_and_login("fake-token")

    assert profile.email == "customer@example.com"
    assert profile.name == "Test Customer"
    assert profile.role == "customer"
    assert tokens.access_token


async def test_returning_user_logs_into_the_same_account(auth, session):
    with patch("app.services.auth_service.google_id_token.verify_oauth2_token", return_value=_claims()):
        _, first = await auth.verify_google_and_login("fake-token-1")
        _, second = await auth.verify_google_and_login("fake-token-2")

    assert first.id == second.id


async def test_a_name_change_in_app_survives_re_login(auth, users, session):
    """Google's profile name must not silently overwrite one the customer
    has since edited in their own profile."""
    with patch("app.services.auth_service.google_id_token.verify_oauth2_token", return_value=_claims()):
        _, profile = await auth.verify_google_and_login("fake-token")

    await users.update_profile(profile.id, {"name": "Edited In App"})
    await session.flush()

    with patch("app.services.auth_service.google_id_token.verify_oauth2_token", return_value=_claims()):
        _, second = await auth.verify_google_and_login("fake-token-again")

    assert second.name == "Edited In App"


async def test_token_for_a_different_app_is_rejected(auth, session):
    """The audience check — this is what stops a token minted for some
    unrelated app from being replayed against this backend."""
    bad_claims = _claims(aud="some-other-apps-client-id")
    with patch("app.services.auth_service.google_id_token.verify_oauth2_token", return_value=bad_claims):
        with pytest.raises(Unauthorized):
            await auth.verify_google_and_login("fake-token")


async def test_a_token_that_fails_googles_own_verification_is_rejected(auth, session):
    with patch(
        "app.services.auth_service.google_id_token.verify_oauth2_token",
        side_effect=ValueError("Token expired"),
    ):
        with pytest.raises(Unauthorized):
            await auth.verify_google_and_login("garbage-token")


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
    with patch("app.services.auth_service.google_id_token.verify_oauth2_token", return_value=_claims()):
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
    with patch("app.services.auth_service.google_id_token.verify_oauth2_token", return_value=_claims()):
        _, profile = await auth.verify_google_and_login("fake-token")

    await users.update_profile(profile.id, {"name": "First"})
    await users.update_profile(profile.id, {"name": "Second"})

    reread = await users.get_by_id(profile.id)
    assert reread["name"] == "Second"
