"""AAD-DATA-006 / AAD-API-002 — the DPDP Act 2023 right-to-erasure endpoint.

`orders.user_id` is (correctly) `ON DELETE RESTRICT` — no order row is ever
deleted here, and no account row is either. What "erasure" means instead:
every saved address gone, the user row's own identifying fields wiped, the
delivery address snapshotted on every past order replaced with a
placeholder, and every refresh token this account holds revoked so a live
session doesn't outlive the request that erased it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.api.deps import Principal
from app.api.v1.routes.auth import delete_me
from app.core.errors import Unauthorized
from app.db.models import Address as AddressRow
from app.db.models import User as UserRow
from app.domain.enums import PaymentMethod, UserStatus
from app.repositories.orders import OrderRepository
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.services.auth_service import AuthService
from tests.test_order_flow import order_request

_CLAIMS = {
    "sub": "erasure_test_sub",
    "email": "erase@example.com",
    "email_verified": True,
    "name": "Erasure Test",
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


@pytest.fixture
async def signed_in_user(auth, session):
    with patch("app.services.auth_service._decode_google_id_token", return_value=_CLAIMS):
        tokens, profile = await auth.verify_google_and_login("fake-token")
    await session.flush()
    return tokens, profile


async def test_erase_wipes_personal_fields_and_deletes_addresses(session, users, signed_in_user):
    _, profile = signed_in_user
    await users.upsert_address(
        profile.id,
        {
            "label": "Home", "line1": "12-3-45 Banjara Hills", "line2": "", "landmark": "",
            "city": "Hyderabad", "pincode": "500034", "latitude": None, "longitude": None,
        },
    )

    await users.erase(profile.id)

    row = await session.get(UserRow, profile.id)
    assert row.name == ""
    assert row.phone is None
    assert row.email is None
    assert row.google_sub is None
    assert row.status == UserStatus.DELETED.value

    remaining = await session.get(UserRow, profile.id)
    assert remaining is not None  # the account row itself is never deleted

    from sqlalchemy import select

    addresses = (
        await session.execute(select(AddressRow).where(AddressRow.user_id == profile.id))
    ).scalars().all()
    assert list(addresses) == []


async def test_erase_blocks_refresh_once_sessions_are_revoked(auth, session, signed_in_user):
    """Erasure alone (without also revoking sessions) would leave an
    already-issued refresh token usable — `status` only gets checked when
    something reads it. `delete_me` calls `logout_all` for exactly this
    reason; this test proves the two together actually close the gap."""
    first, profile = signed_in_user

    await auth.logout_all(profile.id)
    await auth.users.erase(profile.id)

    with pytest.raises(Unauthorized):
        await auth.refresh(first.refresh_token)


async def test_anonymize_addresses_replaces_the_address_but_keeps_the_order(
    order_service, user, orders: OrderRepository, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 2)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    before = await orders.get(order.id)
    assert before["address"]["line1"] == "12-3-45 Banjara Hills"

    touched = await orders.anonymize_addresses_for_user(user["id"])
    assert touched == 1

    after = await orders.get(order.id)
    assert after["status"] == before["status"]  # the order itself is untouched
    assert after["total_paise"] == before["total_paise"]
    assert after["address"]["line1"] != "12-3-45 Banjara Hills"
    # Still a well-formed Address — an OrderView for this order must keep
    # deserializing cleanly after erasure, not start 500ing on read.
    reconstructed = Address(**after["address"])
    assert reconstructed.latitude is None


async def test_delete_me_route_erases_everything_end_to_end(
    session, users, orders, order_service, auth, signed_in_user, milk
):
    first, profile = signed_in_user
    await users.upsert_address(
        profile.id,
        {
            "label": "Home", "line1": "12-3-45 Banjara Hills", "line2": "", "landmark": "",
            "city": "Hyderabad", "pincode": "500034", "latitude": None, "longitude": None,
        },
    )
    order = await order_service.create_order(
        user_id=profile.id,
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )

    principal = Principal(profile.id, "customer")
    await delete_me(principal, users, orders, auth)

    # Session dead.
    with pytest.raises(Unauthorized):
        await auth.refresh(first.refresh_token)

    # Profile anonymised, account row kept.
    row = await session.get(UserRow, profile.id)
    assert row is not None
    assert row.email is None
    assert row.status == UserStatus.DELETED.value

    # Saved address gone.
    from sqlalchemy import select

    addresses = (
        await session.execute(select(AddressRow).where(AddressRow.user_id == profile.id))
    ).scalars().all()
    assert list(addresses) == []

    # Order kept, address anonymised.
    reread = await orders.get(order.id)
    assert reread is not None
    assert reread["status"] == "confirmed"
    assert reread["address"]["line1"] != "12-3-45 Banjara Hills"
