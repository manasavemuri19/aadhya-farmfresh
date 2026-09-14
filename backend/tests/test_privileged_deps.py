"""AAD-SEC-002 — `require_staff` / `require_admin` / `require_delivery_agent`
re-read role and status from the database rather than trusting the JWT's
`role` claim.

Before this fix, `Principal` was built entirely from the token, so demoting
someone (or suspending their account) had no effect until their access
token happened to expire — up to 30 minutes later, and the token itself
could be re-minted from a still-valid refresh token for as long as *that*
lived (60 days, pre-`AAD-SEC-002`). These tests build a `Principal` with a
deliberately stale/wrong role — standing in for a token minted before a
role change — and prove the dependency authorizes on what the database says
now, not what the token claims.
"""

from __future__ import annotations

import pytest

from app.api.deps import Principal, require_admin, require_delivery_agent, require_staff
from app.core.errors import Forbidden, Unauthorized
from app.db.models import User as UserRow
from app.domain.enums import Role, UserStatus
from app.repositories.users import UserRepository


@pytest.fixture
def users(session) -> UserRepository:
    return UserRepository(session)


async def _make_user(users, session, *, role: str, status: str = UserStatus.ACTIVE.value):
    user = await users.get_or_create_by_google(
        google_sub=f"deps_test_sub_{role}_{status}", email="deps@example.com", name="Deps Test",
    )
    row = await session.get(UserRow, user["id"])
    row.role = role
    row.status = status
    await session.flush()
    return user


async def test_require_staff_rejects_a_stale_customer_claim_even_if_db_is_customer(
    users, session
):
    """A token minted while the account was 'customer' presents itself as
    'customer' — this is the ordinary rejection path, included as a sanity
    check that the dependency isn't accidentally permissive."""
    user = await _make_user(users, session, role=Role.CUSTOMER.value)
    stale_principal = Principal(user["id"], role=Role.CUSTOMER.value)

    with pytest.raises(Forbidden):
        await require_staff(stale_principal, users)


async def test_require_staff_trusts_the_database_not_a_stale_token_claim(users, session):
    """The actual point of the fix: a token claiming 'staff' (minted before
    a demotion, or simply forged/stale) must not grant access once the
    database says otherwise."""
    user = await _make_user(users, session, role=Role.CUSTOMER.value)
    forged_or_stale_principal = Principal(user["id"], role=Role.STAFF.value)

    with pytest.raises(Forbidden):
        await require_staff(forged_or_stale_principal, users)


async def test_require_staff_grants_access_once_the_database_says_staff(users, session):
    """The other direction: a token still claiming 'customer' (minted before
    a promotion) must not block someone the database now says is staff —
    proving the dependency genuinely re-reads rather than only ever
    tightening access."""
    user = await _make_user(users, session, role=Role.STAFF.value)
    stale_customer_principal = Principal(user["id"], role=Role.CUSTOMER.value)

    result = await require_staff(stale_customer_principal, users)

    assert result.role == Role.STAFF.value


async def test_require_admin_rejects_staff_that_isnt_admin(users, session):
    user = await _make_user(users, session, role=Role.STAFF.value)
    principal = Principal(user["id"], role=Role.STAFF.value)

    with pytest.raises(Forbidden):
        await require_admin(principal, users)


async def test_require_delivery_agent_rejects_a_customer(users, session):
    user = await _make_user(users, session, role=Role.CUSTOMER.value)
    principal = Principal(user["id"], role=Role.CUSTOMER.value)

    with pytest.raises(Forbidden):
        await require_delivery_agent(principal, users)


async def test_require_staff_rejects_a_suspended_account_even_with_the_right_role(
    users, session
):
    """A demoted-to-suspended staff account keeps a token that still says
    'staff' — status is what must stop it, not role."""
    user = await _make_user(
        users, session, role=Role.STAFF.value, status=UserStatus.SUSPENDED.value
    )
    principal = Principal(user["id"], role=Role.STAFF.value)

    with pytest.raises(Unauthorized):
        await require_staff(principal, users)


async def test_require_staff_rejects_an_unknown_user_id(users):
    """A token whose subject no longer exists in the database at all — the
    account was deleted after the token was issued."""
    principal = Principal("usr_does_not_exist", role=Role.STAFF.value)

    with pytest.raises(Unauthorized):
        await require_staff(principal, users)
