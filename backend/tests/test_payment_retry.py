"""AAD-DATA-005 — a customer whose payment attempt expired, or whose
Payment Link died before its webhook arrived, can retry against the *same*
order instead of cancelling and rebuilding the cart (which would re-reserve
stock that might not still be there). Scoped down from the finding's own
suggested "one-to-many payment_attempts table" — see
OrderService.retry_payment's docstring for the full reasoning — to the
part that actually closes the customer-facing gap: giving the existing
order a fresh gateway order in place.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, Forbidden, NotFound
from app.db.models import Order as OrderRow
from app.db.models import Payment as PaymentRow
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus
from tests.test_order_flow import order_request


async def test_retrying_a_pending_payment_order_issues_a_new_gateway_order(
    order_service, user, session, milk
):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    assert order.status is OrderStatus.PENDING_PAYMENT
    original_provider_order_id = order.payment.provider_order_id

    retried = await order_service.retry_payment(order_id=order.id, user_id=user["id"])

    assert retried.status is OrderStatus.PENDING_PAYMENT  # never a status change
    assert retried.payment.status is PaymentStatus.CREATED
    assert retried.payment.provider_order_id != original_provider_order_id
    assert retried.payment.checkout_payload is not None


async def test_retrying_does_not_touch_stock_or_re_reserve_anything(
    order_service, user, products, milk
):
    from tests.test_order_flow import stock_of

    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    before = await stock_of(products, "MILK-COW-1L")

    await order_service.retry_payment(order_id=order.id, user_id=user["id"])

    after = await stock_of(products, "MILK-COW-1L")
    assert after == before  # the original reservation is untouched either way


async def test_retrying_extends_the_payment_hold(order_service, user, session, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    original_row = await session.get(OrderRow, order.id)
    original_hold = original_row.hold_expires_at

    await order_service.retry_payment(order_id=order.id, user_id=user["id"])

    session.expire(original_row)
    renewed_row = await session.get(OrderRow, order.id)
    assert renewed_row.hold_expires_at > original_hold


async def test_a_cod_order_has_nothing_to_retry(order_service, user, milk):
    """A COD order confirms instantly (never PENDING_PAYMENT), so it's
    refused by the status guard before the payment-method guard is ever
    reached — the COD-specific message is defence in depth for a
    combination (PENDING_PAYMENT + COD) that can't happen through the
    public API today."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    assert order.status is OrderStatus.CONFIRMED
    with pytest.raises(Forbidden, match="no longer waiting on payment"):
        await order_service.retry_payment(order_id=order.id, user_id=user["id"])


async def test_a_confirmed_order_has_nothing_to_retry(order_service, user, orders, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    from app.payments.base import WebhookEvent

    await order_service.apply_webhook(
        WebhookEvent(
            event_id=f"evt_{order.id}", event_type="payment.captured",
            provider_order_id=doc["payment"]["provider_order_id"],
            provider_payment_id=f"pay_{order.id}", amount_paise=order.total_paise, raw={},
        )
    )

    with pytest.raises(Forbidden, match="no longer waiting on payment"):
        await order_service.retry_payment(order_id=order.id, user_id=user["id"])


async def test_a_captured_payment_can_never_be_retried_even_if_order_is_somehow_pending(
    order_service, user, session, milk
):
    """Defence in depth: even if the order's own status were somehow still
    PENDING_PAYMENT (shouldn't happen — capture always confirms it), a
    payment already CAPTURED must never be retried. Retrying money that
    already moved is a second charge, not a retry."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await session.execute(
        PaymentRow.__table__.update()
        .where(PaymentRow.order_id == order.id)
        .values(status=PaymentStatus.CAPTURED.value)
    )
    await session.flush()

    with pytest.raises(Forbidden, match="closer look"):
        await order_service.retry_payment(order_id=order.id, user_id=user["id"])


async def test_an_amount_mismatch_payment_needs_a_human_not_a_retry(
    order_service, user, session, milk
):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await session.execute(
        PaymentRow.__table__.update()
        .where(PaymentRow.order_id == order.id)
        .values(status=PaymentStatus.AMOUNT_MISMATCH.value)
    )
    await session.flush()

    with pytest.raises(Forbidden, match="closer look"):
        await order_service.retry_payment(order_id=order.id, user_id=user["id"])


async def test_repository_guard_allows_a_failed_payment_to_retry_too(
    session, orders, order_service, user, milk
):
    """The repository-level guard also accepts a FAILED payment (not just
    CREATED) — defence in depth for a state combination the current
    webhook flow never actually produces (a `payment.failed`/
    `payment_link.expired` event already cancels the order in the same
    call, per OrderService.apply_webhook), reached here directly rather
    than through the public API, since there is no reachable path that
    leaves an order PENDING_PAYMENT with a FAILED payment today."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await session.execute(
        PaymentRow.__table__.update()
        .where(PaymentRow.order_id == order.id)
        .values(status=PaymentStatus.FAILED.value)
    )
    await session.flush()

    won = await orders.retry_payment(
        order.id,
        provider="mock", provider_order_id="mockord_retry_1",
        checkout_payload={"url": "https://example.test/pay"},
        hold_expires_at=order.created_at,
    )
    assert won is True

    updated = await orders.get(order.id)
    assert updated["payment"]["status"] == PaymentStatus.CREATED.value
    assert updated["payment"]["provider_order_id"] == "mockord_retry_1"


async def test_retrying_a_cancelled_order_is_refused(order_service, user, milk):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await order_service.cancel(order_id=order.id, user_id=user["id"], reason="changed my mind")

    with pytest.raises(Forbidden, match="no longer waiting on payment"):
        await order_service.retry_payment(order_id=order.id, user_id=user["id"])


async def test_service_reports_conflict_when_the_repository_guard_loses_the_race(
    order_service, user, orders, milk, monkeypatch
):
    """The service layer's own check (still PENDING_PAYMENT, still a
    retryable payment status) can pass and then still lose to a real
    concurrent change before the repository's CAS lands — this proves the
    service surfaces that as a clean Conflict, not an unhandled crash or a
    silently-ignored no-op."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )

    async def _always_loses(*args, **kwargs):
        return False

    monkeypatch.setattr(orders, "retry_payment", _always_loses)

    with pytest.raises(Conflict):
        await order_service.retry_payment(order_id=order.id, user_id=user["id"])


async def test_retrying_someone_elses_order_is_not_found(order_service, user, session, milk):
    from app.repositories.users import UserRepository

    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    other = await UserRepository(session).get_or_create_by_google(
        google_sub="retry_other_sub", email="other@example.com", name="Other",
    )
    await session.flush()

    with pytest.raises(NotFound):
        await order_service.retry_payment(order_id=order.id, user_id=other["id"])


async def test_retry_repository_guard_refuses_when_order_left_pending_payment(
    orders, session, order_service, user, milk
):
    """The repository CAS itself: if the order is no longer PENDING_PAYMENT
    at the moment of the write (a race the service-layer check above can't
    fully close), the guard reports failure rather than reviving a payment
    link on an order that moved on."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await session.execute(
        OrderRow.__table__.update().where(OrderRow.id == order.id).values(status="cancelled")
    )
    await session.flush()

    won = await orders.retry_payment(
        order.id, provider="mock", provider_order_id="mockord_should_not_land",
        checkout_payload={}, hold_expires_at=order.created_at,
    )
    assert won is False

    payment_row = (
        await session.execute(select(PaymentRow).where(PaymentRow.order_id == order.id))
    ).scalar_one()
    assert payment_row.provider_order_id != "mockord_should_not_land"
