"""End-to-end checkout behaviour through OrderService."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import StockLedger, Variant as VariantRow
from app.core.errors import Conflict, OutOfStock, PriceChanged
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus
from app.payments.base import WebhookEvent
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

async def stock_of(products, sku: str) -> int:
    result = await products.session.execute(
        select(VariantRow.stock_qty).where(VariantRow.sku == sku)
    )
    return result.scalars().one()


async def write_off_entries(products, order_id: str) -> list[StockLedger]:
    # record_stock_movement only `session.add()`s — this session has
    # autoflush=False (conftest.py), so a pending ledger row needs an
    # explicit flush before a plain select() will see it.
    await products.session.flush()
    result = await products.session.execute(
        select(StockLedger).where(
            StockLedger.order_id == order_id, StockLedger.reason.like("%_write_off")
        )
    )
    return list(result.scalars().all())


ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)


def order_request(lines, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines],
        address=ADDRESS,
        **kw,
    )


async def test_quote_prices_from_the_catalog(order_service, user, milk):
    quote = await order_service.quote([CartLineInput(sku="MILK-COW-1L", qty=2)])
    assert quote.subtotal_paise == 7000
    assert quote.lines[0].unit_price_paise == 3500
    assert quote.eta_minutes == 20


async def test_quote_merges_duplicate_skus(order_service, user, milk):
    quote = await order_service.quote(
        [CartLineInput(sku="MILK-COW-1L", qty=1), CartLineInput(sku="MILK-COW-1L", qty=2)]
    )
    assert len(quote.lines) == 1
    assert quote.lines[0].qty == 3


async def test_quote_flags_unknown_sku_without_failing(order_service, user, milk):
    quote = await order_service.quote([CartLineInput(sku="DOES-NOT-EXIST", qty=1)])
    assert quote.lines[0].unavailable_reason == "not_found"
    assert quote.subtotal_paise == 0


async def test_cod_order_confirms_immediately_and_holds_stock(
    order_service, user, products, milk
):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    assert order.status is OrderStatus.CONFIRMED
    assert order.total_paise == 10_500 + order.delivery_fee_paise

    remaining = await stock_of(products, "MILK-COW-1L")
    assert remaining == 2


async def test_online_order_waits_for_payment(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 3)]),
        idempotency_key=None,
    )
    assert order.status is OrderStatus.PENDING_PAYMENT
    assert order.payment.provider_order_id is not None
    assert order.payment.checkout_payload is not None


async def test_small_order_is_accepted(order_service, user, milk):
    """There is no minimum order — a single litre of milk must check out fine."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    assert order.status is OrderStatus.CONFIRMED
    assert order.subtotal_paise == 3500


async def test_ordering_more_than_stock_is_rejected_and_releases_nothing(
    order_service, user, products, milk
):
    with pytest.raises(OutOfStock):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 9)]),
            idempotency_key=None,
        )
    remaining = await stock_of(products, "MILK-COW-1L")
    assert remaining == 5   # untouched


async def test_client_total_mismatch_is_rejected(order_service, user, milk):
    with pytest.raises(PriceChanged):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 3)], expected_total_paise=1),
            idempotency_key=None,
        )


async def test_idempotency_key_replays_the_same_order(order_service, user, milk):
    request = order_request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD)
    first = await order_service.create_order(
        user_id=user["id"], request=request, idempotency_key="checkout-attempt-0001"
    )
    second = await order_service.create_order(
        user_id=user["id"], request=request, idempotency_key="checkout-attempt-0001"
    )
    assert first.id == second.id


async def test_idempotency_key_reuse_with_a_different_body_is_a_conflict(
    order_service, user, milk
):
    await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD),
        idempotency_key="checkout-attempt-0002",
    )
    # Same key, materially different request body.
    with pytest.raises(Conflict):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request(
                [("MILK-COW-1L", 3)],
                payment_method=PaymentMethod.COD,
                notes="leave at the gate",
            ),
            idempotency_key="checkout-attempt-0002",
        )


async def test_cancelling_returns_stock_exactly_once(order_service, user, products, milk):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    await order_service.cancel(order_id=order.id, user_id=user["id"], reason="changed my mind")

    assert await stock_of(products, "MILK-COW-1L") == 5

    # A second cancel must not credit stock again.
    with pytest.raises(Exception):
        await order_service.cancel(order_id=order.id, user_id=user["id"], reason="again")
    assert await stock_of(products, "MILK-COW-1L") == 5


async def test_cancelling_after_dispatch_does_not_restock(order_service, user, orders, products, milk):
    """AAD-PAY-004: the goods are already on the bike by OUT_FOR_DELIVERY —
    cancelling from there must write the loss off, not credit it back as
    sellable stock (`test_cancelling_returns_stock_exactly_once` above is
    the contrasting case: the same cancel path, before dispatch, *does*
    restock)."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    assert await stock_of(products, "MILK-COW-1L") == 2  # 5 - 3, reserved at checkout

    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.PACKED, note="packed", actor="staff_1"
    )
    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.OUT_FOR_DELIVERY,
        note="out for delivery", actor="agent_1",
    )

    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.CANCELLED,
        note="undeliverable, customer unreachable", actor="agent_1",
    )

    # Not credited back — still reserved-away, exactly as when it left.
    assert await stock_of(products, "MILK-COW-1L") == 2
    entries = await write_off_entries(products, order.id)
    assert len(entries) == 1
    assert entries[0].sku == "MILK-COW-1L"
    assert entries[0].delta == 0
    assert entries[0].reason == "order_cancelled_write_off"


async def test_refunding_a_delivered_order_does_not_restock(
    order_service, user, orders, products, milk
):
    """AAD-PAY-004's other named bug: a goodwill refund after delivery must
    not put the milk back on the shelf — it was already drunk."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 3)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    await order_service.apply_webhook(
        WebhookEvent(
            event_id="evt_deliver_refund", event_type="payment.captured",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id="pay_deliver_refund", amount_paise=order.total_paise, raw={},
        )
    )
    for status in (OrderStatus.PACKED, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED):
        await order_service.update_status(
            order_id=order.id, new_status=status, note=status.value, actor="agent_1"
        )
    assert await stock_of(products, "MILK-COW-1L") == 2  # 5 - 3, never released

    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.REFUNDED,
        note="goodwill refund — spoiled on arrival", actor="staff_1",
    )

    assert await stock_of(products, "MILK-COW-1L") == 2  # still not credited back
    entries = await write_off_entries(products, order.id)
    assert len(entries) == 1
    assert entries[0].reason == "order_refunded_write_off"


async def test_another_user_cannot_cancel_someone_elses_order(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    with pytest.raises(Exception):
        await order_service.cancel(order_id=order.id, user_id="usr_does_not_exist", reason="x")


async def test_capture_webhook_confirms_the_order(order_service, user, orders, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 3)]), idempotency_key=None
    )
    doc = await orders.get(order.id)

    await order_service.apply_webhook(
        WebhookEvent(
            event_id="evt_1", event_type="payment.captured",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id="pay_abc", amount_paise=order.total_paise, raw={},
        )
    )
    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.CONFIRMED.value
    assert updated["payment"]["status"] == PaymentStatus.CAPTURED.value


async def test_capture_with_a_wrong_amount_does_not_confirm(order_service, user, orders, milk):
    """An amount mismatch is a red flag, not a rounding difference — and
    (AAD-PAY-005) not just a log line either: the money captured at the
    gateway must be flagged for a refund, not left stranded against a
    payment row that still says CREATED. See tests/test_amount_mismatch.py
    for the full behaviour (the flag, the ticket, the sweep's refund)."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 3)]), idempotency_key=None
    )
    doc = await orders.get(order.id)

    await order_service.apply_webhook(
        WebhookEvent(
            event_id="evt_2", event_type="payment.captured",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id="pay_bad", amount_paise=1, raw={},
        )
    )
    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.PENDING_PAYMENT.value
    assert updated["payment"]["status"] == PaymentStatus.AMOUNT_MISMATCH.value
    assert updated["payment"]["received_amount_paise"] == 1


async def test_failed_payment_cancels_and_releases_stock(order_service, user, orders, products, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 3)]), idempotency_key=None
    )
    doc = await orders.get(order.id)

    await order_service.apply_webhook(
        WebhookEvent(
            event_id="evt_3", event_type="payment.failed",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id="pay_fail", amount_paise=order.total_paise, raw={},
        )
    )
    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.CANCELLED.value

    assert await stock_of(products, "MILK-COW-1L") == 5


async def test_duplicate_capture_webhook_is_a_no_op(order_service, user, orders, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 3)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    event = WebhookEvent(
        event_id="evt_4", event_type="payment.captured",
        provider_order_id=doc["payment"]["provider_order_id"],
        provider_payment_id="pay_dup", amount_paise=order.total_paise, raw={},
    )
    await order_service.apply_webhook(event)
    await order_service.apply_webhook(event)   # gateways really do this

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.CONFIRMED.value
    confirmations = [
        e for e in updated["timeline"] if e["status"] == OrderStatus.CONFIRMED.value
    ]
    assert len(confirmations) == 1


async def _place_and_capture(order_service, orders, user, lines):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request(lines), idempotency_key=None
    )
    doc = await orders.get(order.id)
    await order_service.apply_webhook(
        WebhookEvent(
            event_id=f"evt_capture_{order.id}", event_type="payment.captured",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id=f"pay_{order.id}", amount_paise=order.total_paise, raw={},
        )
    )
    return await orders.get(order.id)


def _refund_event(order_id: str, doc, *, amount_paise: int | None) -> WebhookEvent:
    return WebhookEvent(
        event_id=f"evt_refund_{order_id}_{amount_paise}", event_type="refund.processed",
        provider_order_id=doc["payment"]["provider_order_id"],
        provider_payment_id=doc["payment"]["provider_payment_id"], amount_paise=amount_paise,
        raw={},
    )


async def test_dashboard_refund_before_dispatch_cancels_and_restocks(
    order_service, user, orders, products, milk
):
    """AAD-PAY-002: a refund issued from the Razorpay dashboard (not through
    our own cancel flow) must still move the order — here, before dispatch,
    that means CONFIRMED -> REFUNDED with stock credited back, same as any
    other pre-dispatch cancel/refund (AAD-PAY-004)."""
    doc = await _place_and_capture(order_service, orders, user, [("MILK-COW-1L", 3)])
    assert doc["status"] == OrderStatus.CONFIRMED.value
    assert await stock_of(products, "MILK-COW-1L") == 2

    await order_service.apply_webhook(
        _refund_event(doc["id"], doc, amount_paise=doc["total_paise"])
    )

    updated = await orders.get(doc["id"])
    assert updated["status"] == OrderStatus.REFUNDED.value
    assert updated["payment"]["status"] == PaymentStatus.REFUNDED.value
    assert await stock_of(products, "MILK-COW-1L") == 5


async def test_dashboard_refund_after_dispatch_writes_off_not_restocks(
    order_service, user, orders, products, milk
):
    """The other half of AAD-PAY-002: the same dashboard refund, but the
    goods are already on the bike (OUT_FOR_DELIVERY) — must still cancel
    the order (so it stops being treated as live) but must write the stock
    off rather than crediting it back (AAD-PAY-004)."""
    doc = await _place_and_capture(order_service, orders, user, [("MILK-COW-1L", 3)])
    for status in (OrderStatus.PACKED, OrderStatus.OUT_FOR_DELIVERY):
        await order_service.update_status(
            order_id=doc["id"], new_status=status, note=status.value, actor="agent_1"
        )
    assert await stock_of(products, "MILK-COW-1L") == 2

    await order_service.apply_webhook(
        _refund_event(doc["id"], doc, amount_paise=doc["total_paise"])
    )

    updated = await orders.get(doc["id"])
    assert updated["status"] == OrderStatus.REFUNDED.value
    assert await stock_of(products, "MILK-COW-1L") == 2  # not credited back
    entries = await write_off_entries(products, doc["id"])
    assert len(entries) == 1
    assert entries[0].reason == "order_refunded_write_off"


async def test_dashboard_refund_does_not_queue_a_second_gateway_refund(
    order_service, user, orders, milk
):
    """The dashboard refund already moved the money — `_cancel`'s
    `_maybe_refund` (AAD-PAY-003) must see the payment as already REFUNDED
    and must not queue another refund call against it."""
    doc = await _place_and_capture(order_service, orders, user, [("MILK-COW-1L", 3)])

    await order_service.apply_webhook(
        _refund_event(doc["id"], doc, amount_paise=doc["total_paise"])
    )

    updated = await orders.get(doc["id"])
    assert updated["payment"]["status"] == PaymentStatus.REFUNDED.value
    pending = await orders.find_pending_refunds()
    assert doc["id"] not in {o["id"] for o in pending}


async def test_partial_refund_does_not_cancel_the_order(order_service, user, orders, milk):
    """A partial refund must not be treated as a cancellation — the
    customer keeps the goods and any balance owed is a manual matter."""
    doc = await _place_and_capture(order_service, orders, user, [("MILK-COW-1L", 3)])

    await order_service.apply_webhook(
        _refund_event(doc["id"], doc, amount_paise=doc["total_paise"] - 100)
    )

    updated = await orders.get(doc["id"])
    assert updated["status"] == OrderStatus.CONFIRMED.value
    assert updated["payment"]["status"] == PaymentStatus.CAPTURED.value


async def test_refund_with_unknown_amount_does_not_cancel_the_order(
    order_service, user, orders, milk
):
    doc = await _place_and_capture(order_service, orders, user, [("MILK-COW-1L", 3)])

    await order_service.apply_webhook(_refund_event(doc["id"], doc, amount_paise=None))

    updated = await orders.get(doc["id"])
    assert updated["status"] == OrderStatus.CONFIRMED.value


async def test_duplicate_dashboard_refund_webhook_is_a_no_op(order_service, user, orders, milk):
    doc = await _place_and_capture(order_service, orders, user, [("MILK-COW-1L", 3)])
    event = _refund_event(doc["id"], doc, amount_paise=doc["total_paise"])

    await order_service.apply_webhook(event)
    await order_service.apply_webhook(event)   # gateways really do this

    updated = await orders.get(doc["id"])
    assert updated["status"] == OrderStatus.REFUNDED.value
    refund_events = [e for e in updated["timeline"] if e["status"] == OrderStatus.REFUNDED.value]
    assert len(refund_events) == 1


async def test_refund_webhook_for_never_captured_payment_does_not_crash(
    order_service, user, orders, milk
):
    """A refund event on an order that was never captured (still
    PENDING_PAYMENT) has no route to REFUNDED (order_state.py deliberately
    excludes it) and nothing was ever charged — it must be logged, not
    acted on, and must never raise."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 3)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    assert doc["status"] == OrderStatus.PENDING_PAYMENT.value

    await order_service.apply_webhook(
        _refund_event(doc["id"], doc, amount_paise=doc["total_paise"])
    )

    updated = await orders.get(doc["id"])
    assert updated["status"] == OrderStatus.PENDING_PAYMENT.value


async def test_made_to_order_item_can_always_be_bought(order_service, user, khoya):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("KHOYA-250G", 1)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    assert order.status is OrderStatus.CONFIRMED
    assert order.eta_minutes == 35
