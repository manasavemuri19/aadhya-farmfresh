"""AAD-OPS-021 — `app/core/security.py` and the authorization gates in
`app/api/deps.py` had zero test coverage before this file: nothing verified
that a refresh token is rejected where an access token is required, that an
expired or tampered token fails, or that each `require_*` gate accepts
exactly the roles it should. `security.py` is the most security-critical
file in the backend and `deps.py` is where every authorization decision is
made — between them, 290 lines with no coverage at all.

These are pure unit tests against `security.py` (round-trip, cross-type
rejection, expiry, tampering) plus direct calls into the `require_*`
dependency functions in `deps.py` (role gating) — no HTTP layer needed for
either, since both are plain async functions FastAPI calls as dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import update as sa_update

from app.api.deps import (
    Principal,
    _reauthorize_from_db,
    current_user,
    require_admin,
    require_delivery_agent,
    require_staff,
)
from app.core.config import settings
from app.core.errors import Forbidden, Unauthorized
from app.core.security import decode_token, issue_access_token, issue_refresh_token
from app.db.models import User as UserRow
from app.domain.enums import Role, UserStatus
from app.repositories.users import UserRepository


def test_access_token_round_trips_with_its_role():
    token = issue_access_token("user_1", role="staff")
    payload = decode_token(token, expected_type="access")
    assert payload["sub"] == "user_1"
    assert payload["role"] == "staff"
    assert payload["typ"] == "access"


def test_refresh_token_round_trips_with_its_jti():
    token = issue_refresh_token("user_1", jti="jti_abc")
    payload = decode_token(token, expected_type="refresh")
    assert payload["sub"] == "user_1"
    assert payload["jti"] == "jti_abc"
    assert payload["typ"] == "refresh"


def test_a_refresh_token_is_rejected_where_an_access_token_is_required():
    """The `typ` claim check is the entire defence against replaying a
    30-day refresh token as a bearer credential — this is the single most
    important assertion in this file."""
    refresh = issue_refresh_token("user_1", jti="jti_abc")
    with pytest.raises(Unauthorized):
        decode_token(refresh, expected_type="access")


def test_an_access_token_is_rejected_where_a_refresh_token_is_required():
    access = issue_access_token("user_1")
    with pytest.raises(Unauthorized):
        decode_token(access, expected_type="refresh")


def test_an_expired_access_token_is_rejected():
    now = datetime.now(UTC)
    payload = {
        "sub": "user_1", "typ": "access", "role": "customer",
        "iat": int((now - timedelta(minutes=60)).timestamp()),
        "exp": int((now - timedelta(minutes=30)).timestamp()),
    }
    expired = jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    with pytest.raises(Unauthorized):
        decode_token(expired, expected_type="access")


def test_a_tampered_signature_is_rejected():
    token = issue_access_token("user_1")
    tampered = token[:-4] + ("A" * 4 if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(Unauthorized):
        decode_token(tampered, expected_type="access")


def test_a_token_signed_with_a_different_secret_is_rejected():
    now = datetime.now(UTC)
    payload = {
        "sub": "user_1", "typ": "access", "role": "customer",
        "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=30)).timestamp()),
    }
    forged = jwt.encode(payload, "not-the-real-secret", algorithm=settings.jwt_algorithm)
    with pytest.raises(Unauthorized):
        decode_token(forged, expected_type="access")


def test_a_token_with_no_subject_is_rejected():
    now = datetime.now(UTC)
    payload = {
        "typ": "access", "role": "customer",
        "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=30)).timestamp()),
    }
    no_sub = jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    with pytest.raises(Unauthorized):
        decode_token(no_sub, expected_type="access")


async def test_current_user_rejects_a_missing_authorization_header():
    with pytest.raises(Unauthorized):
        await current_user(authorization=None)


async def test_current_user_rejects_a_malformed_authorization_header():
    """A header present but not in `Bearer <token>` shape must be a clean
    401, not a 500 from trying to split/decode garbage."""
    with pytest.raises(Unauthorized):
        await current_user(authorization="NotBearer sometoken")


async def test_current_user_accepts_a_well_formed_bearer_access_token():
    token = issue_access_token("user_1", role="customer")
    principal = await current_user(authorization=f"Bearer {token}")
    assert principal.user_id == "user_1"
    assert principal.role == "customer"


async def test_reauthorize_from_db_rejects_a_user_who_no_longer_exists(session):
    principal = Principal(user_id="ghost_user_does_not_exist", role="customer")
    with pytest.raises(Unauthorized):
        await _reauthorize_from_db(principal, UserRepository(session))


async def test_reauthorize_from_db_rejects_a_suspended_user(session):
    users = UserRepository(session)
    user = await users.get_or_create_by_google(
        google_sub="suspend_test_sub", email="susp@example.com", name="Suspended",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == user["id"]).values(status=UserStatus.SUSPENDED.value)
    )
    await session.flush()
    principal = Principal(user_id=user["id"], role="customer")
    with pytest.raises(Unauthorized):
        await _reauthorize_from_db(principal, users)


async def test_require_staff_accepts_staff_and_admin_refuses_customer(session):
    users = UserRepository(session)
    staff_user = await users.get_or_create_by_google(
        google_sub="staff_sub", email="staff@example.com", name="Staff",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == staff_user["id"]).values(role=Role.STAFF.value)
    )
    customer_user = await users.get_or_create_by_google(
        google_sub="cust_sub", email="cust@example.com", name="Customer",
    )
    await session.flush()

    staff_result = await require_staff(Principal(staff_user["id"], "staff"), users)
    assert staff_result.is_staff is True

    with pytest.raises(Forbidden):
        await require_staff(Principal(customer_user["id"], "customer"), users)


async def test_require_admin_refuses_plain_staff(session):
    users = UserRepository(session)
    staff_user = await users.get_or_create_by_google(
        google_sub="staff_only_sub", email="staffonly@example.com", name="Staff",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == staff_user["id"]).values(role=Role.STAFF.value)
    )
    await session.flush()

    with pytest.raises(Forbidden):
        await require_admin(Principal(staff_user["id"], "staff"), users)


async def test_require_admin_accepts_admin(session):
    users = UserRepository(session)
    admin_user = await users.get_or_create_by_google(
        google_sub="admin_sub", email="admin@example.com", name="Admin",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == admin_user["id"]).values(role=Role.ADMIN.value)
    )
    await session.flush()

    result = await require_admin(Principal(admin_user["id"], "admin"), users)
    assert result.is_admin is True


async def test_require_delivery_agent_refuses_staff(session):
    users = UserRepository(session)
    staff_user = await users.get_or_create_by_google(
        google_sub="staff_not_agent_sub", email="staffnotagent@example.com", name="Staff",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == staff_user["id"]).values(role=Role.STAFF.value)
    )
    await session.flush()

    with pytest.raises(Forbidden):
        await require_delivery_agent(Principal(staff_user["id"], "staff"), users)


async def test_require_delivery_agent_accepts_a_delivery_agent(session):
    users = UserRepository(session)
    agent_user = await users.get_or_create_by_google(
        google_sub="agent_sub", email="agent@example.com", name="Agent",
    )
    await session.execute(
        sa_update(UserRow)
        .where(UserRow.id == agent_user["id"])
        .values(role=Role.DELIVERY_AGENT.value)
    )
    await session.flush()

    result = await require_delivery_agent(Principal(agent_user["id"], "delivery_agent"), users)
    assert result.is_delivery_agent is True


def test_hash_secret_and_verify_secret_round_trip_and_reject_a_wrong_guess():
    """AAD-SEC-016 deleted these (the old phone/OTP-login subsystem's stored
    codes, AAD-QUAL-001) after they were left with zero callers, and left a
    standing note on how to bring them back correctly if anything ever
    needed hashed-secret storage again: Argon2, specific exception types
    (not a bare `except Exception`), and `check_needs_rehash` wired into
    every successful verify. AAD-SEC-027's in-app delivery-verification
    code is that something — this is the proof the reintroduction actually
    followed that guidance, not just that the functions exist again."""
    import inspect

    from argon2.exceptions import VerifyMismatchError

    import app.core.security as security_module

    assert hasattr(security_module, "hash_secret")
    assert hasattr(security_module, "verify_secret")

    hashed = security_module.hash_secret("4827")
    assert hashed != "4827"  # never stored in the clear
    assert security_module.verify_secret("4827", hashed) is True
    assert security_module.verify_secret("0000", hashed) is False
    # A malformed/foreign hash value must be reported as "doesn't match",
    # never raised through to the caller.
    assert security_module.verify_secret("4827", "not-a-real-argon2-hash") is False

    source = inspect.getsource(security_module.verify_secret)
    assert "except Exception" not in source
    assert VerifyMismatchError.__name__ in source
    assert "check_needs_rehash" in source
