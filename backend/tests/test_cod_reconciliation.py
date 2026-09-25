"""AAD-BIZ-004: COD cash reconciliation.

AAD-SEC-027's delivery-OTP mechanism itself (valid/wrong/expired/locked-out/
reused codes, duplicate delivery attempts, ownership checks) is already
covered end to end by tests/test_batch35_delivery_otp.py — 14 tests, still
green after this change (see the full suite run). Nothing here re-tests that
ground; this file is only the new COD half: what happens to the cash the
instant that same code is verified for a COD order, and the settlement flow
an admin uses to reconcile it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.core.errors import Conflict, ValidationError
from app.core.ids import new_id
from app.db.models import CodCollection
from app.db.models import Order as OrderRow
from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus
from app.payments.base import WebhookEvent
from app.payments.mock import MockPaymentProvider
from app.repositories.cash import CashRepository
from app.repositories.delivery import DeliveryRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.cash_service import CashService
from app.services.delivery_service import DeliveryService
from app.services.order_service import OrderService

DEFAULT_ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034",
)


def order_request(lines, *, address: Address = DEFAULT_ADDRESS, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines],
        address=address,
        **kw,
    )


@pytest.fixture
async def agent(session):
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="cod_agent_sub_0001", email="cod-agent@example.com", name="COD Agent",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == record["id"]).values(role="delivery_agent")
    )
    await session.flush()
    record["role"] = "delivery_agent"
    return record


@pytest.fixture
async def other_agent(session):
    """A second agent, purely to keep each agent's pending cash separate in
    the settlement tests — a settlement must never sweep up another
    agent's unclaimed collections."""
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="cod_agent_sub_0002", email="cod-agent-2@example.com", name="COD Agent Two",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == record["id"]).values(role="delivery_agent")
    )
    await session.flush()
    record["role"] = "delivery_agent"
    return record


@pytest.fixture
async def admin(session):
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="cod_admin_sub_0001", email="owner@example.com", name="Owner",
    )
    await session.execute(
        update(UserRow).where(UserRow.id == record["id"]).values(role="admin")
    )
    await session.flush()
    record["role"] = "admin"
    return record


@pytest.fixture
def delivery_repo(session) -> DeliveryRepository:
    return DeliveryRepository(session)


@pytest.fixture
def users_repo(session) -> UserRepository:
    return UserRepository(session)


@pytest.fixture
def cash_repo(session) -> CashRepository:
    return CashRepository(session)


@pytest.fixture
def order_service(session, products, orders, cash_repo) -> OrderService:
    """Overrides conftest's own `order_service` fixture for this file only
    (standard pytest fixture shadowing) — the shared one deliberately
    leaves users/push/support/cash unwired for order-flow tests that don't
    need them (see its own definition); every test here is specifically
    about the `cash` wiring, so it has to be present."""
    return OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider(), cash=cash_repo
    )


@pytest.fixture
def delivery_service(delivery_repo, users_repo, order_service) -> DeliveryService:
    return DeliveryService(delivery_repo, users_repo, order_service)


@pytest.fixture
def cash_service(cash_repo, users_repo) -> CashService:
    return CashService(cash_repo, users_repo)


async def _advance_to_out_for_delivery(delivery_service, order_id, agent_id) -> None:
    await delivery_service.accept(order_id, agent_id)
    await delivery_service.update_status(order_id, agent_id, OrderStatus.PACKED)
    await delivery_service.update_status(order_id, agent_id, OrderStatus.OUT_FOR_DELIVERY)


async def _code_for(session, order_id) -> str:
    await session.flush()
    row = await session.get(OrderRow, order_id)
    return row.delivery_otp_plain


async def _collections_for_order(session, order_id) -> list[CodCollection]:
    await session.flush()
    result = await session.execute(select(CodCollection).where(CodCollection.order_id == order_id))
    return list(result.scalars().all())


async def _deliver_cod_order(order_service, delivery_service, session, agent_id, order_id) -> None:
    """Places nothing — advances an already-created COD order through to a
    verified delivery, the shared setup every collection/settlement test
    below needs."""
    await _advance_to_out_for_delivery(delivery_service, order_id, agent_id)
    code = await _code_for(session, order_id)
    await delivery_service.verify_delivery(order_id, agent_id, code)


async def _confirm_online_order(order_service, orders, order) -> None:
    """Pushes a normal (non-COD) order from pending_payment to confirmed via
    a mock capture webhook — the same pattern test_order_flow.py's own
    payment tests use, reused here only as setup for the "prepaid order is
    unaffected" test below."""
    doc = await orders.get(order.id)
    await order_service.apply_webhook(
        WebhookEvent(
            event_id=f"evt_{order.id}", event_type="payment.captured",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id=f"pay_{order.id}", amount_paise=order.total_paise, raw={},
        )
    )


# ---------- COD collection ----------


async def test_cod_collection_recorded_when_delivery_verified(
    order_service, delivery_service, agent, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 2)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-collect-1",
    )
    await _deliver_cod_order(order_service, delivery_service, session, agent["id"], order.id)

    rows = await _collections_for_order(session, order.id)
    assert len(rows) == 1
    assert rows[0].agent_id == agent["id"]
    assert rows[0].amount_paise == order.total_paise
    assert rows[0].settlement_id is None

    updated = await order_service.get_for_user(order.id, user["id"])
    assert updated.payment.status == PaymentStatus.CAPTURED


async def test_duplicate_collection_is_prevented(
    order_service, delivery_service, agent, user, milk, session, cash_repo
):
    """The OTP's own single-use guarantee already stops a second
    verify_delivery call from ever reaching the collection code again (see
    test_batch35_delivery_otp.py's reused-code coverage) — this proves the
    second, structural guard underneath that: cod_collections.order_id is
    UNIQUE, so even a direct second attempt to record the same order's cash
    is absorbed, not duplicated."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-collect-2",
    )
    await _deliver_cod_order(order_service, delivery_service, session, agent["id"], order.id)

    again = await cash_repo.record_collection(
        collection_id=new_id("codc", 20),
        order_id=order.id,
        agent_id=agent["id"],
        amount_paise=order.total_paise,
    )
    assert again is None

    rows = await _collections_for_order(session, order.id)
    assert len(rows) == 1


async def test_duplicate_delivery_attempt_does_not_double_collect(
    order_service, delivery_service, agent, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-collect-3",
    )
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])
    code = await _code_for(session, order.id)
    await delivery_service.verify_delivery(order.id, agent["id"], code)

    with pytest.raises(Conflict):
        await delivery_service.verify_delivery(order.id, agent["id"], code)

    rows = await _collections_for_order(session, order.id)
    assert len(rows) == 1


async def test_prepaid_order_is_unaffected(
    order_service, delivery_service, orders, agent, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)]),  # default: PaymentMethod.ONLINE
        idempotency_key="cod-collect-4",
    )
    await _confirm_online_order(order_service, orders, order)
    await _advance_to_out_for_delivery(delivery_service, order.id, agent["id"])
    code = await _code_for(session, order.id)
    await delivery_service.verify_delivery(order.id, agent["id"], code)

    rows = await _collections_for_order(session, order.id)
    assert rows == []

    updated = await order_service.get_for_user(order.id, user["id"])
    assert updated.payment.method == PaymentMethod.ONLINE
    assert updated.status == OrderStatus.DELIVERED


# ---------- settlement ----------


async def test_exact_cash_settlement(
    order_service, delivery_service, cash_service, agent, admin, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 2)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-settle-1",
    )
    await _deliver_cod_order(order_service, delivery_service, session, agent["id"], order.id)

    settlement = await cash_service.settle_agent(
        agent_id=agent["id"],
        actual_amount_paise=order.total_paise,
        reason="",
        actor_id=admin["id"],
    )

    assert settlement.status.value == "settled"
    assert settlement.expected_amount_paise == order.total_paise
    assert settlement.actual_amount_paise == order.total_paise
    assert settlement.discrepancy_paise == 0
    assert settlement.orders_settled == 1

    rows = await _collections_for_order(session, order.id)
    assert rows[0].settlement_id == settlement.id


async def test_short_cash_is_a_discrepancy_not_a_silent_settle(
    order_service, delivery_service, cash_service, agent, admin, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 2)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-settle-2",
    )
    await _deliver_cod_order(order_service, delivery_service, session, agent["id"], order.id)

    short_by = 100
    with pytest.raises(ValidationError):
        # A mismatch with no reason is rejected outright — never quietly
        # accepted as settled just because a number was supplied.
        await cash_service.settle_agent(
            agent_id=agent["id"],
            actual_amount_paise=order.total_paise - short_by,
            reason="",
            actor_id=admin["id"],
        )

    settlement = await cash_service.settle_agent(
        agent_id=agent["id"],
        actual_amount_paise=order.total_paise - short_by,
        reason="Agent says a customer paid ₹1 less in exact change",
        actor_id=admin["id"],
    )

    assert settlement.status.value == "discrepancy"
    assert settlement.discrepancy_paise == -short_by
    assert settlement.reason

    rows = await _collections_for_order(session, order.id)
    assert rows[0].settlement_id == settlement.id  # still claimed, just flagged


async def test_extra_cash_is_also_a_discrepancy(
    order_service, delivery_service, cash_service, agent, admin, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 2)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-settle-3",
    )
    await _deliver_cod_order(order_service, delivery_service, session, agent["id"], order.id)

    over_by = 50
    settlement = await cash_service.settle_agent(
        agent_id=agent["id"],
        actual_amount_paise=order.total_paise + over_by,
        reason="Customer rounded up, didn't want change back",
        actor_id=admin["id"],
    )

    assert settlement.status.value == "discrepancy"
    assert settlement.discrepancy_paise == over_by


async def test_duplicate_settlement_is_prevented(
    order_service, delivery_service, cash_service, agent, admin, user, milk, session
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-settle-4",
    )
    await _deliver_cod_order(order_service, delivery_service, session, agent["id"], order.id)

    await cash_service.settle_agent(
        agent_id=agent["id"], actual_amount_paise=order.total_paise,
        reason="", actor_id=admin["id"],
    )

    with pytest.raises(Conflict):
        # Everything this agent had was already claimed by the settlement
        # above — a second attempt must find nothing pending, not re-settle
        # (or, worse, double-count) the same cash.
        await cash_service.settle_agent(
            agent_id=agent["id"], actual_amount_paise=0, reason="", actor_id=admin["id"],
        )


async def test_settlement_only_claims_that_agents_own_collections(
    order_service, delivery_service, cash_service, agent, other_agent, admin, user, milk, session
):
    order_a = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-settle-5a",
    )
    await _deliver_cod_order(order_service, delivery_service, session, agent["id"], order_a.id)

    order_b = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="cod-settle-5b",
    )
    await _deliver_cod_order(
        order_service, delivery_service, session, other_agent["id"], order_b.id
    )

    settlement = await cash_service.settle_agent(
        agent_id=agent["id"], actual_amount_paise=order_a.total_paise,
        reason="", actor_id=admin["id"],
    )
    assert settlement.orders_settled == 1

    # other_agent's own collection is untouched and still pending.
    other_rows = await _collections_for_order(session, order_b.id)
    assert other_rows[0].settlement_id is None
