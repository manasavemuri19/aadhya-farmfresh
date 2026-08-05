"""End-to-end checkout behaviour through OrderService."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Variant as VariantRow
from app.core.errors import Conflict, OutOfStock, PriceChanged, ValidationError
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus
from app.payments.base import WebhookEvent
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

async def stock_of(products, sku: str) -> int:
    result = await products.session.execute(
        select(VariantRow.stock_qty).where(VariantRow.sku == sku)
    )
    return result.scalars().one()


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


async def test_order_below_minimum_is_rejected(order_service, user, milk):
    with pytest.raises(ValidationError):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)]),  # Rs 35, under the Rs 99 floor
            idempotency_key=None,
        )


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
    """An amount mismatch is a red flag, not a rounding difference."""
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


async def test_made_to_order_item_can_always_be_bought(order_service, user, khoya):
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("KHOYA-250G", 1)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    assert order.status is OrderStatus.CONFIRMED
    assert order.eta_minutes == 35
