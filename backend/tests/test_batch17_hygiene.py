"""Batch 17: agent-location freshness (AAD-SEC-033), push-token format and
lifecycle (AAD-SEC-031 / AAD-SEC-032), checkout_payload no longer leaking
forever (AAD-PERF-009), a typed mock-payment body (AAD-QUAL-024), a typed
set-availability body (AAD-API-007), and the stock_ledger -> orders FK
(AAD-DATA-014).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import PushToken as PushTokenRow
from app.db.models import StockLedger as StockLedgerRow
from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod
from app.repositories.push_tokens import PushTokenRepository
from app.repositories.users import UserRepository
from app.schemas.notifications import RegisterPushToken
from app.schemas.order import MockCompletePayment, SetAvailabilityRequest
from app.services.order_service import OrderService
from tests.test_order_service_hygiene import order_request  # reuse the helper

# ---------- AAD-SEC-033: agent-location freshness ----------


async def test_agent_location_hidden_once_stale(session, order_service, user, milk):
    svc = OrderService(
        order_service.products, order_service.orders, order_service.idempotency,
        order_service.payments, users=UserRepository(session),
    )
    agent = await UserRepository(session).get_or_create_by_google(
        google_sub="sec033_agent", email="sec033-agent@example.com", name="Agent",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == agent["id"]).values(role="delivery_agent")
    )
    await session.flush()

    from app.repositories.delivery import DeliveryRepository
    from app.services.delivery_service import DeliveryService

    order = await svc.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="sec033-stale-loc",
    )
    delivery_service = DeliveryService(DeliveryRepository(session), UserRepository(session), svc)
    await delivery_service.accept(order.id, agent["id"])
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.OUT_FOR_DELIVERY)

    # Report a location, then force it stale by rewriting last_location_at
    # directly (an update() bulk statement's onupdate would refresh
    # updated_at, not the domain column we actually care about here).
    await UserRepository(session).update_agent_location(
        agent["id"], latitude=17.43, longitude=78.45
    )
    stale = datetime.now(UTC) - timedelta(minutes=10)
    await session.execute(
        update(UserRow).where(UserRow.id == agent["id"]).values(last_location_at=stale)
    )
    await session.flush()

    detail = await svc.get_for_user(order.id, user["id"])
    assert detail.delivery_agent_location is None


async def test_agent_location_shown_while_fresh(session, order_service, user, milk):
    svc = OrderService(
        order_service.products, order_service.orders, order_service.idempotency,
        order_service.payments, users=UserRepository(session),
    )
    agent = await UserRepository(session).get_or_create_by_google(
        google_sub="sec033_fresh_agent", email="sec033-fresh@example.com", name="Agent",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == agent["id"]).values(role="delivery_agent")
    )
    await session.flush()

    from app.repositories.delivery import DeliveryRepository
    from app.services.delivery_service import DeliveryService

    order = await svc.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="sec033-fresh-loc",
    )
    delivery_service = DeliveryService(DeliveryRepository(session), UserRepository(session), svc)
    await delivery_service.accept(order.id, agent["id"])
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.OUT_FOR_DELIVERY)
    await UserRepository(session).update_agent_location(
        agent["id"], latitude=17.43, longitude=78.45
    )

    detail = await svc.get_for_user(order.id, user["id"])
    assert detail.delivery_agent_location is not None


# ---------- AAD-SEC-031: push-token format validation ----------


def test_register_push_token_rejects_a_non_expo_shaped_string():
    with pytest.raises(PydanticValidationError):
        RegisterPushToken(token="not-a-real-push-token-at-all", platform="android")


def test_register_push_token_accepts_the_real_expo_shape():
    tok = RegisterPushToken(
        token="ExponentPushToken[abcDEF123_-xyz]", platform="android"
    )
    assert tok.token.startswith("ExponentPushToken[")


# ---------- AAD-SEC-032: push-token deregistration + pruning ----------


async def test_delete_for_user_removes_only_the_caller_own_token(session, user):
    other = await UserRepository(session).get_or_create_by_google(
        google_sub="sec032_other", email="sec032-other@example.com", name="Other",
    )
    await session.flush()
    repo = PushTokenRepository(session)
    await repo.register(
        user_id=user["id"], token="ExponentPushToken[mine12345]", platform="android"
    )
    await repo.register(
        user_id=other["id"], token="ExponentPushToken[theirs6789]", platform="android"
    )
    await session.flush()

    # Wrong owner: nothing deleted.
    deleted = await repo.delete_for_user(
        user_id=user["id"], token="ExponentPushToken[theirs6789]"
    )
    assert deleted is False
    assert await repo.list_for_users([other["id"]]) == ["ExponentPushToken[theirs6789]"]

    # Right owner: gone.
    deleted = await repo.delete_for_user(
        user_id=user["id"], token="ExponentPushToken[mine12345]"
    )
    assert deleted is True
    assert await repo.list_for_users([user["id"]]) == []


async def test_prune_stale_removes_only_untouched_tokens(session, user):
    repo = PushTokenRepository(session)
    await repo.register(
        user_id=user["id"], token="ExponentPushToken[fresh00001]", platform="android"
    )
    await repo.register(
        user_id=user["id"], token="ExponentPushToken[old0000001]", platform="android"
    )
    await session.flush()

    old = datetime.now(UTC) - timedelta(days=91)
    await session.execute(
        update(PushTokenRow)
        .where(PushTokenRow.token == "ExponentPushToken[old0000001]")
        .values(updated_at=old)
    )
    await session.flush()

    pruned = await repo.prune_stale()
    assert pruned == 1
    remaining = await repo.list_for_users([user["id"]])
    assert remaining == ["ExponentPushToken[fresh00001]"]


async def test_register_bumps_updated_at_on_a_repeat_registration(session, user):
    """AAD-SEC-032 side effect: on_conflict_do_update's set_ clause doesn't
    get the column's onupdate default for free (same shape as AAD-DATA-002)
    — register() now sets it explicitly."""
    repo = PushTokenRepository(session)
    token = "ExponentPushToken[repeat0001]"
    await repo.register(user_id=user["id"], token=token, platform="android")
    await session.flush()

    old = datetime.now(UTC) - timedelta(days=91)
    await session.execute(
        update(PushTokenRow).where(PushTokenRow.token == token).values(updated_at=old)
    )
    await session.flush()

    await repo.register(user_id=user["id"], token=token, platform="android")
    await session.flush()

    pruned = await repo.prune_stale()
    assert pruned == 0  # re-registering counted as "touched", so it survives the sweep


# ---------- AAD-PERF-009: checkout_payload not echoed forever ----------


async def test_checkout_payload_present_at_creation_absent_on_later_read(
    order_service, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.ONLINE),
        idempotency_key="perf009-create",
    )
    assert order.payment.checkout_payload is not None

    reread = await order_service.get_for_user(order.id, user["id"])
    assert reread.payment.checkout_payload is None


# ---------- AAD-QUAL-024: typed mock-complete body ----------


def test_mock_complete_payment_requires_order_id():
    with pytest.raises(PydanticValidationError):
        MockCompletePayment()  # type: ignore[call-arg]


def test_mock_complete_payment_rejects_an_unknown_outcome():
    with pytest.raises(PydanticValidationError):
        MockCompletePayment(order_id="ord_x", outcome="maybe")


def test_mock_complete_payment_defaults_outcome_to_success():
    body = MockCompletePayment(order_id="ord_x")
    assert body.outcome == "success"


# ---------- AAD-API-007: typed set-availability body ----------


def test_set_availability_request_requires_active():
    with pytest.raises(PydanticValidationError):
        SetAvailabilityRequest()  # type: ignore[call-arg]


def test_set_availability_request_accepts_a_bool():
    assert SetAvailabilityRequest(active=False).active is False


# ---------- AAD-DATA-014: stock_ledger -> orders FK ----------


async def test_database_rejects_a_stock_ledger_row_with_no_such_order(engine, session):
    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            bad_session.add(
                StockLedgerRow(
                    sku="MILK-COW-1L", delta=-1, reason="test", order_id="ord_doesnotexist"
                )
            )
            await bad_session.flush()


async def test_database_allows_a_stock_ledger_row_with_no_order(session):
    session.add(StockLedgerRow(sku="MILK-COW-1L", delta=-1, reason="admin_adjustment"))
    await session.flush()  # order_id stays NULL — must not raise
