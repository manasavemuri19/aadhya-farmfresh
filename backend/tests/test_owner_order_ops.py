"""AAD-BIZ-006 / AAD-PAY-021: owner-only cancellation from the admin route,
and the stuck-refund owner alert in the housekeeping sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.api.deps import Principal
from app.api.v1.routes import admin as admin_routes
from app.core.errors import Forbidden
from app.db.models import Payment
from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus
from app.payments.base import WebhookEvent
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest, UpdateOrderStatusRequest
from app.services.order_service import STUCK_REFUND_ALERT_AFTER

ADDRESS = Address(label="Home", line1="1 Farm Rd", city="Hyderabad", pincode="500034")


def _request(method: PaymentMethod) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku="MILK-COW-1L", qty=1)], address=ADDRESS, payment_method=method
    )


async def _captured_order(order_service, orders, user):
    order = await order_service.create_order(
        user_id=user["id"], request=_request(PaymentMethod.ONLINE), idempotency_key=None
    )
    doc = await orders.get(order.id)
    await order_service.apply_webhook(
        WebhookEvent(
            event_id=f"evt_{order.id}", event_type="payment_link.paid",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id=f"pay_{order.id}", amount_paise=order.total_paise, raw={},
        )
    )
    return order


# ---------- AAD-BIZ-006: cancelling is owner-only ----------


async def test_a_non_owner_principal_cannot_cancel_an_order(order_service, user, milk):
    """Defence in depth: the route also checks the role inline, on top of the
    StaffUser dependency (which is owner-only since AAD-BIZ-007)."""
    order = await order_service.create_order(
        user_id=user["id"], request=_request(PaymentMethod.COD), idempotency_key=None
    )
    with pytest.raises(Forbidden):
        await admin_routes.update_order_status(
            order.id,
            UpdateOrderStatusRequest(status=OrderStatus.CANCELLED, note="no"),
            Principal("usr_staff", "staff"),
            order_service,
        )


async def test_owner_cancelling_a_paid_order_queues_the_refund(
    order_service, orders, user, milk
):
    order = await _captured_order(order_service, orders, user)
    view = await admin_routes.update_order_status(
        order.id,
        UpdateOrderStatusRequest(status=OrderStatus.CANCELLED, note="customer called"),
        Principal("usr_owner", "admin"),
        order_service,
    )
    assert view.status == OrderStatus.CANCELLED.value
    assert view.payment.status == PaymentStatus.REFUND_PENDING.value


async def test_owner_can_do_ordinary_fulfilment(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=_request(PaymentMethod.COD), idempotency_key=None
    )
    view = await admin_routes.update_order_status(
        order.id,
        UpdateOrderStatusRequest(status=OrderStatus.PACKED, note=""),
        Principal("usr_owner", "admin"),
        order_service,
    )
    assert view.status == OrderStatus.PACKED.value


# ---------- AAD-PAY-021: stuck refunds ----------


@pytest.fixture
async def owner(session):
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="owner_sub_021", email="owner-021@example.com", name="Owner"
    )
    await session.execute(update(UserRow).where(UserRow.id == record["id"]).values(role="admin"))
    await session.flush()
    return record


async def _queue_refund(order_service, orders, user, session, *, age: timedelta):
    order = await _captured_order(order_service, orders, user)
    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.CANCELLED, note="x", actor="owner"
    )
    await session.execute(
        update(Payment)
        .where(Payment.order_id == order.id)
        .values(updated_at=datetime.now(UTC) - age)
    )
    await session.flush()
    return order


async def test_list_refunds_pending_flags_only_old_ones_as_stuck(
    order_service, orders, user, milk, session
):
    fresh = await _queue_refund(order_service, orders, user, session, age=timedelta(minutes=1))
    old = await _queue_refund(
        order_service, orders, user, session, age=STUCK_REFUND_ALERT_AFTER + timedelta(minutes=5)
    )
    rows = {r.order_id: r for r in await order_service.list_refunds_pending()}
    assert rows[fresh.id].stuck is False
    assert rows[old.id].stuck is True


async def test_sweep_alerts_owner_once_per_stuck_refund(
    order_service, orders, user, owner, milk, session, monkeypatch
):
    from app import main

    sent: list[dict] = []

    async def fake_notify(self, user_ids, *, title, body, data=None):
        sent.append({"user_ids": user_ids, "title": title, "body": body})

    monkeypatch.setattr(main.PushService, "notify_users", fake_notify)

    stuck = await _queue_refund(
        order_service, orders, user, session, age=STUCK_REFUND_ALERT_AFTER + timedelta(minutes=1)
    )
    await _queue_refund(order_service, orders, user, session, age=timedelta(minutes=2))

    # defer_until_commit runs effects immediately when no outbox batch is open.
    assert await main._alert_stuck_refunds(session) == 1
    assert len(sent) == 1
    assert sent[0]["user_ids"] == [owner["id"]]
    assert stuck.order_number in sent[0]["body"]

    # Debounced: a second pass doesn't re-alert the same refund.
    assert await main._alert_stuck_refunds(session) == 0
    assert len(sent) == 1


# ---------- owner order history + COD cash visibility + post-delivery refund ----------


async def _delivered_cod_order(order_service, user, session, agent):
    """Places a COD order and walks it to a verified delivery by `agent`."""
    from app.db.models import Order as OrderRow
    from app.repositories.delivery import DeliveryRepository
    from app.services.delivery_service import DeliveryService

    users = order_service.users
    delivery = DeliveryService(DeliveryRepository(session), users, order_service)
    order = await order_service.create_order(
        user_id=user["id"], request=_request(PaymentMethod.COD), idempotency_key=None
    )
    await delivery.accept(order.id, agent["id"])
    await delivery.update_status(order.id, agent["id"], OrderStatus.PACKED)
    await delivery.update_status(order.id, agent["id"], OrderStatus.OUT_FOR_DELIVERY)
    await session.flush()
    code = (await session.get(OrderRow, order.id)).delivery_otp_plain
    await delivery.verify_delivery(order.id, agent["id"], code)
    return order


@pytest.fixture
def staff_order_service(session, products, orders):
    """conftest's order_service leaves users/cash unwired; the owner views
    need both (names + COD collections)."""
    from app.payments.mock import MockPaymentProvider
    from app.repositories.cash import CashRepository
    from app.repositories.idempotency import IdempotencyRepository
    from app.services.order_service import OrderService

    return OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider(),
        users=UserRepository(session), cash=CashRepository(session),
    )


@pytest.fixture
async def agent(session):
    record = await UserRepository(session).get_or_create_by_google(
        google_sub="agent_sub_hist", email="agent-hist@example.com", name="Ravi"
    )
    await session.execute(
        update(UserRow).where(UserRow.id == record["id"]).values(role="delivery_agent")
    )
    await session.flush()
    return record


async def test_delivered_cod_order_shows_where_its_cash_is(
    staff_order_service, user, agent, milk, session
):
    order = await _delivered_cod_order(staff_order_service, user, session, agent)

    page = await staff_order_service.list_queue_for_staff(
        [OrderStatus.DELIVERED], newest_first=True
    )
    row = next(v for v in page.items if v.id == order.id)
    assert row.cod_cash is not None
    assert row.cod_cash.agent_name == "Ravi"
    assert row.cod_cash.amount_paise == order.total_paise
    assert row.cod_cash.settled is False
    assert row.delivery_code is None

    detail = await staff_order_service.get_for_staff(order.id)
    assert detail.cod_cash is not None and detail.cod_cash.settled is False


async def test_history_is_newest_first_and_paginates(
    staff_order_service, user, milk, session
):
    ids = []
    for _ in range(3):
        o = await staff_order_service.create_order(
            user_id=user["id"], request=_request(PaymentMethod.COD), idempotency_key=None
        )
        await staff_order_service.update_status(
            order_id=o.id, new_status=OrderStatus.CANCELLED, note="x", actor="owner"
        )
        ids.append(o.id)

    # Real orders come from separate transactions and so get distinct
    # created_at values; here all three share one transaction's now(), so
    # space them out explicitly — the cursor is created_at, ties aren't a
    # real-world case.
    from app.db.models import Order as OrderRow

    for age, oid in zip((30, 20, 10), ids, strict=True):
        await session.execute(
            update(OrderRow).where(OrderRow.id == oid)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=age))
        )
    await session.flush()

    page1 = await staff_order_service.list_queue_for_staff(
        [OrderStatus.CANCELLED], limit=2, newest_first=True
    )
    assert [v.id for v in page1.items] == [ids[2], ids[1]]
    assert page1.has_more is True

    page2 = await staff_order_service.list_queue_for_staff(
        [OrderStatus.CANCELLED], limit=2, newest_first=True,
        after=datetime.fromisoformat(page1.next_cursor),
    )
    assert [v.id for v in page2.items] == [ids[0]]
    assert page2.has_more is False


async def test_owner_can_refund_a_paid_order_after_delivery(order_service, orders, user, milk):
    order = await _captured_order(order_service, orders, user)
    for status in (OrderStatus.PACKED, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED):
        await order_service.update_status(
            order_id=order.id, new_status=status, note="", actor="staff"
        )
    view = await admin_routes.update_order_status(
        order.id,
        UpdateOrderStatusRequest(status=OrderStatus.REFUNDED, note="spoiled milk"),
        Principal("usr_owner", "admin"),
        order_service,
    )
    assert view.status == OrderStatus.REFUNDED.value
    assert view.payment.status == PaymentStatus.REFUND_PENDING.value
    assert view.delivery_code is None


async def test_going_out_for_delivery_does_not_leak_the_code_to_staff(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=_request(PaymentMethod.COD), idempotency_key=None
    )
    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.PACKED, note="", actor="staff"
    )
    view = await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.OUT_FOR_DELIVERY, note="", actor="staff"
    )
    assert view.delivery_code is None
    # ...while the customer's own read still shows it.
    mine = await order_service.get_for_user(order.id, user["id"])
    assert mine.delivery_code is not None
