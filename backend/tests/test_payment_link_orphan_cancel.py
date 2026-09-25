"""AAD-PAY-016 — the gateway call in `_create_order_inner` (and its sibling
in `retry_payment`) happens deliberately *before* the local write that
reserves stock / attaches the new gateway order to the row — see
`OrderService`'s own module docstring for why: a slow gateway must never
hold inventory locks. That ordering means a failure between the gateway
call and the write it precedes — stock disappearing concurrently, or (for
a retry) losing the compare-and-swap to another change — used to leave a
real, live, payable Razorpay Payment Link with nothing in this app pointing
at it at all. If it were paid in that window, the money would be captured
against an order that was never written, and nothing would ever notice.

These tests prove the fix: on that exact failure, the orphaned link is
best-effort cancelled before the original error propagates. Both service-
layer tests reproduce the race by monkeypatching the repository call that
sits *after* the gateway call to fail/lose exactly the way a real
concurrent change would, rather than trying to win an actual database race
— the point being proven is "does the service clean up after that failure",
which doesn't need the race itself to be real. The provider-level tests
prove `RazorpayProvider.cancel_order` itself: it calls the right SDK method,
and never raises regardless of how Razorpay responds, since it always runs
from inside an already-failing path.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from razorpay.errors import BadRequestError, ServerError

from app.core.errors import Conflict, OutOfStock
from app.domain.enums import OrderStatus
from app.payments.razorpay import RazorpayProvider
from tests.test_order_flow import order_request

# ---------- RazorpayProvider.cancel_order itself ----------


@pytest.fixture
def provider(monkeypatch) -> RazorpayProvider:
    from app.core.config import settings

    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr("fake-test-secret"))
    monkeypatch.setattr(settings, "razorpay_webhook_secret", SecretStr("fake-webhook-secret"))
    monkeypatch.setattr(
        settings, "razorpay_callback_url", "https://example.test/v1/payments/link-redirect"
    )
    return RazorpayProvider()


async def test_cancel_order_calls_the_payment_link_cancel_endpoint(provider, monkeypatch):
    seen = {}

    def fake_cancel(payment_link_id):
        seen["id"] = payment_link_id
        return {"id": payment_link_id, "status": "cancelled"}

    monkeypatch.setattr(provider._client.payment_link, "cancel", fake_cancel)

    await provider.cancel_order(provider_order_id="plink_orphaned123")

    assert seen["id"] == "plink_orphaned123"


async def test_cancel_order_swallows_a_bad_request_error(provider, monkeypatch, caplog):
    """Razorpay refuses to cancel a link that's already been paid — the
    single case this fix can't close (see the provider's own docstring).
    That must not raise here: this runs from inside an already-failing
    path, and a second exception would replace the real error the caller
    is propagating instead of just failing to help."""

    def broken_cancel(payment_link_id):
        raise BadRequestError("This Payment Link has already been paid.")

    monkeypatch.setattr(provider._client.payment_link, "cancel", broken_cancel)

    await provider.cancel_order(provider_order_id="plink_already_paid")  # must not raise

    assert "razorpay refused to cancel" in caplog.text


async def test_cancel_order_swallows_a_transient_failure(provider, monkeypatch, caplog):
    def broken_cancel(payment_link_id):
        raise ServerError("Razorpay is having a bad day.")

    monkeypatch.setattr(provider._client.payment_link, "cancel", broken_cancel)

    await provider.cancel_order(provider_order_id="plink_x")  # must not raise

    assert "could not reach razorpay" in caplog.text


# ---------- OrderService._create_order_inner ----------


async def test_stock_vanishing_after_the_gateway_call_cancels_the_orphaned_link(
    order_service, user, products, milk, monkeypatch
):
    """The earlier availability check in `_create_order_inner` passes (the
    cart looks fine when priced), the gateway call succeeds, and only then
    does `reserve_stock_bulk` discover the stock is gone — exactly the
    shape of a real concurrent sale, reproduced here by monkeypatching the
    repository call directly rather than winning an actual race."""

    async def _sold_out_after_all(items):
        return {sku: False for sku, _qty in items}

    monkeypatch.setattr(products, "reserve_stock_bulk", _sold_out_after_all)

    with pytest.raises(OutOfStock):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)]),
            idempotency_key=None,
        )

    # The OutOfStock above is unchanged — this is the new behaviour: the
    # Payment Link the gateway already created for this attempt was
    # cancelled rather than left live and payable with no order behind it.
    assert len(order_service.payments.cancelled_order_ids) == 1


async def test_a_cod_order_has_no_gateway_order_to_cancel(
    order_service, user, products, milk, monkeypatch
):
    """COD never calls the gateway at all (`_create_order_inner` only does
    for an online order) — the same stock-vanishes failure here must not
    crash reaching for a `provider_order` that was never created."""

    async def _sold_out_after_all(items):
        return {sku: False for sku, _qty in items}

    monkeypatch.setattr(products, "reserve_stock_bulk", _sold_out_after_all)

    from app.domain.enums import PaymentMethod

    with pytest.raises(OutOfStock):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
            idempotency_key=None,
        )

    assert order_service.payments.cancelled_order_ids == []


async def test_a_genuine_order_still_has_nothing_cancelled(order_service, user, milk):
    """Sanity check the fixture/mock wiring itself: an ordinary successful
    online order must not trigger any cancellation — only the failure path
    does."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)]),
        idempotency_key=None,
    )

    assert order.status is OrderStatus.PENDING_PAYMENT
    assert order_service.payments.cancelled_order_ids == []


# ---------- OrderService.retry_payment ----------


async def test_losing_the_retry_cas_cancels_the_orphaned_link(
    order_service, user, orders, milk, monkeypatch
):
    """Same shape as `test_service_reports_conflict_when_the_repository_
    guard_loses_the_race` in test_payment_retry.py — a fresh Payment Link is
    created for the retry, and then the repository's own compare-and-swap
    loses to a real concurrent change before it can attach that link to the
    order. The Conflict this already raised is unchanged; the new behaviour
    is that the now-orphaned link is cancelled first."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )

    async def _always_loses(*args, **kwargs):
        return False

    monkeypatch.setattr(orders, "retry_payment", _always_loses)

    with pytest.raises(Conflict):
        await order_service.retry_payment(order_id=order.id, user_id=user["id"])

    # Two links exist by now at the (mock) gateway: the original one from
    # create_order, still live and untouched, and the retry's own, which
    # this failure must have cancelled.
    assert len(order_service.payments.cancelled_order_ids) == 1
    assert order_service.payments.cancelled_order_ids[0] != order.payment.provider_order_id
