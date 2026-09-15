"""AAD-PAY-003 — the gateway refund call no longer runs inside the request
transaction that cancels an order and releases its stock; it runs later,
from `OrderService.process_pending_refunds`, called by the periodic
sweeper (`app/main.py`) in its own transaction, the same place
`release_expired_holds` already makes its own gateway calls outside any
customer-facing request.

`test_late_capture_refund.py` already proves the queueing half: a
cancel/force-refund flags the payment `refund_pending` and returns without
ever touching the gateway. This file proves the sweep that actually calls
it — including the correctness bug this finding named directly: catching
only `UpstreamError` around an inline gateway call meant any *other*
exception rolled back a refund that may have already succeeded. Now that
the call happens in the sweeper's own shared transaction across a whole
batch, the equivalent failure mode is one order's exception rolling back
every other order's refund from the same sweep — so the catch has to be
broad, and each order has to be independent of the others.
"""

from __future__ import annotations

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


async def _queue_a_refund(order_service, orders, user, sku: str) -> str:
    """Places, pays and cancels an order the same way the late-capture path
    does, leaving its payment `refund_pending` — the realistic starting
    point for every test below, and the same queuing path
    `test_late_capture_refund.py` covers directly."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([(sku, 1)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    await order_service.apply_webhook(
        WebhookEvent(
            event_id=f"evt_{order.id}",
            event_type="payment_link.paid",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id=f"pay_{order.id}",
            amount_paise=order.total_paise,
            raw={},
        )
    )
    await order_service.cancel(order_id=order.id, user_id=user["id"], reason="changed my mind")
    doc = await orders.get(order.id)
    assert doc["payment"]["status"] == PaymentStatus.REFUND_PENDING.value
    return order.id


async def test_process_pending_refunds_calls_the_gateway_and_marks_refunded(
    order_service, orders, user, milk, monkeypatch
):
    order_id = await _queue_a_refund(order_service, orders, user, "MILK-COW-1L")
    doc = await orders.get(order_id)
    expected_provider_payment_id = doc["payment"]["provider_payment_id"]

    calls = []

    async def fake_refund(*, provider_payment_id, amount_paise, notes):
        calls.append((provider_payment_id, amount_paise, notes))
        return "rfnd_fake"

    monkeypatch.setattr(order_service.payments, "refund", fake_refund)

    refunded_count = await order_service.process_pending_refunds()

    assert refunded_count == 1
    assert len(calls) == 1
    called_payment_id, called_amount, _ = calls[0]
    assert called_payment_id == expected_provider_payment_id
    assert called_amount == doc["total_paise"]

    updated = await orders.get(order_id)
    assert updated["payment"]["status"] == PaymentStatus.REFUNDED.value


async def test_one_orders_gateway_failure_does_not_block_another_orders_refund(
    order_service, orders, user, milk, monkeypatch
):
    """The core correctness property this finding named: refunding order A
    and order B in the same sweep, with A's gateway call failing, must
    still leave B refunded — not roll B's write back along with A's."""
    milk.variants[0].stock_qty = 5  # enough for two 1-unit orders from the fixture's SKU
    ok_id = await _queue_a_refund(order_service, orders, user, "MILK-COW-1L")
    fails_id = await _queue_a_refund(order_service, orders, user, "MILK-COW-1L")

    async def selective_refund(*, provider_payment_id, amount_paise, notes):
        if notes["order_id"] == fails_id:
            raise Exception("gateway had a genuinely unexpected failure")
        return "rfnd_ok"

    monkeypatch.setattr(order_service.payments, "refund", selective_refund)

    refunded_count = await order_service.process_pending_refunds()

    assert refunded_count == 1
    ok_doc = await orders.get(ok_id)
    fails_doc = await orders.get(fails_id)
    assert ok_doc["payment"]["status"] == PaymentStatus.REFUNDED.value
    # Left exactly where it was — visible and retried next sweep, not lost.
    assert fails_doc["payment"]["status"] == PaymentStatus.REFUND_PENDING.value


async def test_a_non_upstream_exception_is_caught_not_just_upstream_error(
    order_service, orders, user, milk, monkeypatch
):
    """Before this fix, the inline refund call caught only `UpstreamError` —
    exactly the class of bug this test targets: a `KeyError` or similar from
    a malformed gateway response must not escape and take the whole sweep
    down with it (and, in the old inline-transaction shape, roll back the
    cancel/stock-release that had already committed)."""
    order_id = await _queue_a_refund(order_service, orders, user, "MILK-COW-1L")

    async def broken_refund(*, provider_payment_id, amount_paise, notes):
        raise KeyError("malformed gateway response")

    monkeypatch.setattr(order_service.payments, "refund", broken_refund)

    refunded_count = await order_service.process_pending_refunds()  # must not raise

    assert refunded_count == 0
    doc = await orders.get(order_id)
    assert doc["status"] == OrderStatus.CANCELLED.value  # the cancel itself is untouched
    assert doc["payment"]["status"] == PaymentStatus.REFUND_PENDING.value


async def test_process_pending_refunds_is_a_quiet_noop_with_nothing_queued(order_service):
    assert await order_service.process_pending_refunds() == 0
