"""AAD-PAY-001 — a capture that lands after the order was already cancelled
must trigger an automatic refund, not sit CAPTURED against a CANCELLED order
forever.

The poll-and-reconcile backstop added for AAD-PAY-006 already narrows the
race this finding describes to a brief window around the sweep itself (it
used to be the entire 15-minute hold). This is the remaining defense: what
happens when a capture genuinely does land after cancellation anyway.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db.models import Order as OrderRow
from app.db.models import Variant as VariantRow
from app.domain.enums import OrderStatus, PaymentStatus
from app.payments.base import WebhookEvent
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)


def order_request(lines, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines], address=ADDRESS, **kw
    )


async def stock_of(products, sku: str) -> int:
    result = await products.session.execute(
        select(VariantRow.stock_qty).where(VariantRow.sku == sku)
    )
    return result.scalars().one()


async def _expire_hold(session, order_id: str) -> None:
    await session.execute(
        update(OrderRow)
        .where(OrderRow.id == order_id)
        .values(hold_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.flush()


async def _place_and_cancel_via_sweep(order_service, session, user, milk):
    """The realistic setup: an order whose hold expired with no webhook and
    no redirect, cancelled by the sweep (MockPaymentProvider.poll_status is
    always None, so the sweep has nothing to reconcile against and cancels
    for real) — then a capture arrives anyway, late."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    await _expire_hold(session, order.id)
    released = await order_service.release_expired_holds()
    assert released == 1
    return order


async def test_late_capture_on_cancelled_order_refunds_automatically(
    order_service, session, user, orders, products, milk
):
    order = await _place_and_cancel_via_sweep(order_service, session, user, milk)
    doc = await orders.get(order.id)
    assert doc["status"] == OrderStatus.CANCELLED.value
    assert await stock_of(products, "MILK-COW-1L") == 5  # released once, by the sweep

    await order_service.apply_webhook(
        WebhookEvent(
            event_id="evt_late_capture_1",
            event_type="payment_link.paid",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id="pay_late_arrival",
            amount_paise=order.total_paise,
            raw={},
        )
    )

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.REFUNDED.value
    # AAD-PAY-003: the gateway call is no longer made inline here — it's
    # queued for the sweeper, so the payment is refund_pending immediately
    # after the webhook, not yet refunded. See
    # test_pending_refund_sweep.py for the sweep actually completing it.
    assert updated["payment"]["status"] == PaymentStatus.REFUND_PENDING.value
    # Stock must not be credited a second time for the same order.
    assert await stock_of(products, "MILK-COW-1L") == 5
    refund_events = [e for e in updated["timeline"] if e["status"] == OrderStatus.REFUNDED.value]
    assert len(refund_events) == 1


async def test_late_capture_queues_a_refund_rather_than_calling_the_gateway_inline(
    order_service, session, user, orders, products, milk, monkeypatch
):
    """AAD-PAY-003's whole point: this must never call the gateway from
    inside the same transaction that just cancelled/refunded the order and
    released its stock. If it did, this test's `payments.refund` stub —
    which unconditionally raises — would blow up the webhook handler
    itself; instead the handler must complete normally and simply leave the
    payment queued for the sweeper."""
    order = await _place_and_cancel_via_sweep(order_service, session, user, milk)
    doc = await orders.get(order.id)

    async def refund_must_not_be_called(*, provider_payment_id, amount_paise, notes):
        raise AssertionError("_maybe_refund must not call the gateway inline (AAD-PAY-003)")

    monkeypatch.setattr(order_service.payments, "refund", refund_must_not_be_called)

    await order_service.apply_webhook(
        WebhookEvent(
            event_id="evt_late_capture_2",
            event_type="payment_link.paid",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id="pay_late_arrival_2",
            amount_paise=order.total_paise,
            raw={},
        )
    )

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.REFUNDED.value
    assert updated["payment"]["status"] == PaymentStatus.REFUND_PENDING.value


async def test_duplicate_late_capture_after_refund_is_a_quiet_noop(
    order_service, session, user, orders, products, milk
):
    order = await _place_and_cancel_via_sweep(order_service, session, user, milk)
    doc = await orders.get(order.id)
    event = WebhookEvent(
        event_id="evt_late_capture_3",
        event_type="payment_link.paid",
        provider_order_id=doc["payment"]["provider_order_id"],
        provider_payment_id="pay_late_arrival_3",
        amount_paise=order.total_paise,
        raw={},
    )
    await order_service.apply_webhook(event)  # refunds it
    await order_service.apply_webhook(event)  # gateways really do redeliver

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.REFUNDED.value
    assert await stock_of(products, "MILK-COW-1L") == 5
    refund_events = [e for e in updated["timeline"] if e["status"] == OrderStatus.REFUNDED.value]
    assert len(refund_events) == 1  # not refunded/transitioned a second time


async def test_duplicate_capture_on_a_confirmed_order_is_a_quiet_noop(
    order_service, user, orders, products, milk
):
    """The other half of the finding: a capture redelivered for an order
    that is already CONFIRMED (the ordinary case) must stay a no-op — no
    refund, no second transition, stock untouched."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    event = WebhookEvent(
        event_id="evt_confirm_dup",
        event_type="payment_link.paid",
        provider_order_id=doc["payment"]["provider_order_id"],
        provider_payment_id="pay_confirm",
        amount_paise=order.total_paise,
        raw={},
    )
    await order_service.apply_webhook(event)
    confirmed = await orders.get(order.id)
    assert confirmed["status"] == OrderStatus.CONFIRMED.value

    # Redelivered — same event, order already confirmed.
    await order_service.apply_webhook(event)

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.CONFIRMED.value
    assert updated["payment"]["status"] == PaymentStatus.CAPTURED.value
    assert await stock_of(products, "MILK-COW-1L") == 3  # still reserved, not released
