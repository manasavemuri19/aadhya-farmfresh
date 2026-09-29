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


async def test_staff_cannot_cancel_an_order(order_service, user, milk):
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


async def test_staff_can_still_do_ordinary_fulfilment(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=_request(PaymentMethod.COD), idempotency_key=None
    )
    view = await admin_routes.update_order_status(
        order.id,
        UpdateOrderStatusRequest(status=OrderStatus.PACKED, note=""),
        Principal("usr_staff", "staff"),
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
