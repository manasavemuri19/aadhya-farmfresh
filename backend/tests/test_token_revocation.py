"""AAD-SEC-002 — refresh-token rotation, reuse detection and revocation.

Before this fix, nothing issued could ever be invalidated: no `jti`, no
token store, no logout. These tests exercise `AuthService` directly against
a real Postgres session (the same pattern `test_google_auth.py` uses),
proving the actual rotate-on-use, reuse-revokes-the-family, logout, and
logout-all behaviour described in the fix.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.errors import Unauthorized
from app.db.models import User as UserRow
from app.domain.enums import UserStatus
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.services.auth_service import AuthService

_CLAIMS = {
    "sub": "revocation_test_sub",
    "email": "revoke@example.com",
    "email_verified": True,
    "name": "Revoke Test",
    "aud": "test-web-client-id",
}


@pytest.fixture(autouse=True)
def google_client_id(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "google_web_client_id", "test-web-client-id")


@pytest.fixture
def users(session) -> UserRepository:
    return UserRepository(session)


@pytest.fixture
def refresh_tokens(session) -> RefreshTokenRepository:
    return RefreshTokenRepository(session)


@pytest.fixture
def auth(users, refresh_tokens) -> AuthService:
    return AuthService(users, refresh_tokens)


async def _sign_in(auth, session, *, token: str = "fake-token"):
    with patch("app.services.auth_service._decode_google_id_token", return_value=_CLAIMS):
        tokens, profile = await auth.verify_google_and_login(token)
    await session.flush()
    return tokens, profile


@pytest.fixture
async def signed_in_user(auth, session):
    """A real user plus a real, stored, first-issued token pair — exactly
    what a successful sign-in produces."""
    return await _sign_in(auth, session)


async def test_refresh_rotates_and_the_old_token_no_longer_works(auth, signed_in_user):
    first, _ = signed_in_user

    second = await auth.refresh(first.refresh_token)

    assert second.refresh_token != first.refresh_token
    with pytest.raises(Unauthorized):
        await auth.refresh(first.refresh_token)


async def test_refresh_reuse_revokes_the_entire_session_family(auth, signed_in_user):
    """The core of the fix: presenting an already-rotated token again is not
    a routine expired-session error — it revokes every live session for the
    user, including the one issued by the legitimate rotation."""
    first, _ = signed_in_user
    second = await auth.refresh(first.refresh_token)

    # The stolen/duplicated old token is replayed.
    with pytest.raises(Unauthorized):
        await auth.refresh(first.refresh_token)

    # The legitimate holder's current token is now dead too — by design:
    # the safe response to a possible theft is to force everyone back
    # through sign-in, not just the token that was replayed.
    with pytest.raises(Unauthorized):
        await auth.refresh(second.refresh_token)


async def test_logout_revokes_only_that_session(auth, session, signed_in_user):
    """A second, independent sign-in for the same user (a second device) —
    logging out of the first must not touch the second."""
    first, profile = signed_in_user
    second_device, _ = await _sign_in(auth, session, token="fake-token-device-2")

    await auth.logout(first.refresh_token)

    with pytest.raises(Unauthorized):
        await auth.refresh(first.refresh_token)
    # The other device's session is untouched.
    renewed = await auth.refresh(second_device.refresh_token)
    assert renewed.access_token


async def test_logout_all_revokes_every_session(auth, session, signed_in_user):
    first, profile = signed_in_user
    second_device, _ = await _sign_in(auth, session, token="fake-token-device-2")

    await auth.logout_all(profile.id)

    with pytest.raises(Unauthorized):
        await auth.refresh(first.refresh_token)
    with pytest.raises(Unauthorized):
        await auth.refresh(second_device.refresh_token)


async def test_logout_is_idempotent_and_tolerates_a_garbage_token(auth, signed_in_user):
    first, _ = signed_in_user

    await auth.logout(first.refresh_token)
    await auth.logout(first.refresh_token)  # already revoked — must not raise
    await auth.logout("not-even-a-jwt")  # garbage — must not raise


async def test_refresh_for_a_suspended_account_is_rejected(auth, session, signed_in_user):
    first, profile = signed_in_user

    row = await session.get(UserRow, profile.id)
    row.status = UserStatus.SUSPENDED.value
    await session.flush()

    with pytest.raises(Unauthorized):
        await auth.refresh(first.refresh_token)
