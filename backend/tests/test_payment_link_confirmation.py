"""AAD-PAY-007 / AAD-PAY-006 — Payment Link webhook confirmation and the
poll-and-reconcile backstop.

`test_razorpay_payment_link.py` already covers the callback signature
formula in isolation. This file covers the three defects that made the
webhook itself unable to confirm a Payment Link payment (AAD-PAY-007), and
the redesign that stops order confirmation from depending on the customer's
browser completing a redirect (AAD-PAY-006).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.db.models import Order as OrderRow
from app.db.models import Variant as VariantRow
from app.domain.enums import OrderStatus, PaymentStatus
from app.payments.base import WebhookEvent
from app.payments.mock import MockPaymentProvider
from app.payments.razorpay import RazorpayProvider
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

FAKE_KEY_SECRET = "fake-key-secret-for-verification-only"
FAKE_WEBHOOK_SECRET = "fake-webhook-secret-for-verification-only"

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


@pytest.fixture
def provider(monkeypatch) -> RazorpayProvider:
    from app.core.config import settings

    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", SecretStr(FAKE_KEY_SECRET))
    monkeypatch.setattr(settings, "razorpay_webhook_secret", SecretStr(FAKE_WEBHOOK_SECRET))
    return RazorpayProvider()


def _webhook_body(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    signature = hmac.new(FAKE_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, signature


# ---------------------------------------------------------------------------
# AAD-PAY-007 defect 1 & 2 — entity resolution and event classification
# ---------------------------------------------------------------------------


class TestParseWebhookPaymentLinkEvents:
    def test_payment_link_paid_is_read_and_classified_as_a_capture(self, provider):
        """Before the fix, `payload.payment_link.entity` was never consulted,
        so this event resolved to an empty entity and was silently dropped —
        `provider_order_id` came back `None` and `apply_webhook` never saw it."""
        payload = {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": "plink_ABC123",
                        "reference_id": "ord_our_own_id",
                        "amount": 10_500,
                        "amount_paid": 10_500,
                        "status": "paid",
                    }
                },
                # Real Razorpay payloads nest the underlying payment's own
                # Order alongside the link — its order_id belongs to that
                # internal Order object, never to anything this app stored.
                "payment": {
                    "entity": {
                        "id": "pay_XYZ789",
                        "order_id": "order_internal_to_razorpay_not_ours",
                        "amount": 10_500,
                    }
                },
            },
        }
        body, signature = _webhook_body(payload)
        event = provider.parse_webhook(body=body, signature=signature)

        assert event.is_capture is True
        # The payment link's own id — what create_order stored as
        # provider_order_id — not the nested payment entity's order_id.
        assert event.provider_order_id == "plink_ABC123"
        # Our own order id, recovered directly from reference_id.
        assert event.order_id == "ord_our_own_id"
        assert event.provider_payment_id == "pay_XYZ789"
        assert event.amount_paise == 10_500

    def test_payment_link_expired_and_cancelled_are_classified_as_failures(self, provider):
        for status_event in ("payment_link.expired", "payment_link.cancelled"):
            payload = {
                "event": status_event,
                "payload": {
                    "payment_link": {
                        "entity": {
                            "id": "plink_ABC123",
                            "reference_id": "ord_our_own_id",
                            "amount": 10_500,
                            "status": status_event.split(".")[1],
                        }
                    }
                },
            }
            body, signature = _webhook_body(payload)
            event = provider.parse_webhook(body=body, signature=signature)
            assert event.is_failure is True, status_event
            assert event.is_capture is False, status_event

    def test_payment_link_partially_paid_is_routed_through_capture_not_ignored(self, provider):
        """Partial payment is disabled at link creation, so this should not
        occur in practice — but if it does, it must not be silently dropped.
        Routing it through is_capture means OrderService's existing
        amount-mismatch guard turns it into a logged incident instead."""
        payload = {
            "event": "payment_link.partially_paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": "plink_ABC123",
                        "reference_id": "ord_our_own_id",
                        "amount": 10_500,
                        "amount_paid": 4_000,
                        "status": "partially_paid",
                    }
                }
            },
        }
        body, signature = _webhook_body(payload)
        event = provider.parse_webhook(body=body, signature=signature)
        assert event.is_capture is True
        assert event.amount_paise == 4_000

    def test_standard_payment_captured_event_is_unaffected(self, provider):
        """The pre-existing Standard Checkout shape (no payment_link entity
        at all) must still resolve exactly as before."""
        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {"id": "pay_XYZ789", "order_id": "order_ABC", "amount": 10_500}
                }
            },
        }
        body, signature = _webhook_body(payload)
        event = provider.parse_webhook(body=body, signature=signature)
        assert event.is_capture is True
        assert event.provider_order_id == "order_ABC"
        assert event.provider_payment_id == "pay_XYZ789"
        assert event.order_id is None

    def test_bad_signature_still_raises(self, provider):
        from app.core.errors import PaymentFailed

        body = json.dumps({"event": "payment_link.paid", "payload": {}}).encode()
        with pytest.raises(PaymentFailed):
            provider.parse_webhook(body=body, signature="0" * 64)


# ---------------------------------------------------------------------------
# AAD-PAY-007 defect 3 — end-to-end confirmation via reference_id/order_id
# ---------------------------------------------------------------------------


async def test_payment_link_paid_webhook_confirms_the_order_via_order_id(
    order_service, user, orders, milk
):
    """This is the scenario that was completely broken: a real Payment Link
    webhook, matched the way Razorpay actually sends it, must confirm the
    order — not just an event carrying a provider_order_id that happens to
    already match."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    provider_order_id = doc["payment"]["provider_order_id"]

    event = WebhookEvent(
        event_id="evt_link_1",
        event_type="payment_link.paid",
        # Deliberately wrong provider_order_id — simulates the old defect,
        # where the code derived the wrong id from the payment entity. The
        # fix must not depend on this field being right when order_id is set.
        provider_order_id="not_the_real_provider_order_id",
        provider_payment_id="pay_abc",
        amount_paise=order.total_paise,
        raw={},
        order_id=order.id,
    )
    await order_service.apply_webhook(event)

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.CONFIRMED.value
    assert updated["payment"]["status"] == PaymentStatus.CAPTURED.value
    assert provider_order_id  # sanity: the order did have one, just unused here


async def test_webhook_without_order_id_still_falls_back_to_provider_order_id(
    order_service, user, orders, milk
):
    """Non-Payment-Link events (order_id is None) must keep working exactly
    as before the fix."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    doc = await orders.get(order.id)

    event = WebhookEvent(
        event_id="evt_std_1",
        event_type="payment.captured",
        provider_order_id=doc["payment"]["provider_order_id"],
        provider_payment_id="pay_def",
        amount_paise=order.total_paise,
        raw={},
    )
    await order_service.apply_webhook(event)

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.CONFIRMED.value


# ---------------------------------------------------------------------------
# AAD-PAY-006 — poll-and-reconcile backstop before the sweeper cancels
# ---------------------------------------------------------------------------


class _StubProviderPaidOnPoll(MockPaymentProvider):
    """A payment gateway stand-in whose webhook/redirect never arrived, but
    which reports the payment as paid when polled directly."""

    async def poll_status(self, *, provider_order_id: str) -> WebhookEvent | None:
        return WebhookEvent(
            event_id=f"poll_{provider_order_id}",
            event_type="payment_link.paid",
            provider_order_id=provider_order_id,
            provider_payment_id="pay_polled",
            amount_paise=None,
            raw={},
            order_id=self.order_id_for_poll,
        )


class _StubProviderUnpaidOnPoll(MockPaymentProvider):
    async def poll_status(self, *, provider_order_id: str) -> WebhookEvent | None:
        return None


class _StubProviderGatewayUnreachable(MockPaymentProvider):
    async def poll_status(self, *, provider_order_id: str) -> WebhookEvent | None:
        from app.core.errors import UpstreamError

        raise UpstreamError("gateway is down")


async def _expire_hold(session, order_id: str) -> None:
    await session.execute(
        update(OrderRow)
        .where(OrderRow.id == order_id)
        .values(hold_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.flush()


async def test_sweep_confirms_instead_of_cancelling_when_gateway_reports_paid(
    session, products, orders, user, milk
):
    from app.repositories.idempotency import IdempotencyRepository
    from app.services.order_service import OrderService

    provider = _StubProviderPaidOnPoll()
    service = OrderService(products, orders, IdempotencyRepository(session), provider)

    order = await service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    provider.order_id_for_poll = order.id
    await _expire_hold(session, order.id)

    released = await service.release_expired_holds()

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.CONFIRMED.value
    assert released == 0  # nothing was actually released — it was confirmed
    # Stock stays reserved for a confirmed order, not returned to the shelf.
    assert await stock_of(products, "MILK-COW-1L") == 3


async def test_sweep_still_cancels_when_gateway_confirms_unpaid(
    session, products, orders, user, milk
):
    from app.repositories.idempotency import IdempotencyRepository
    from app.services.order_service import OrderService

    service = OrderService(
        products, orders, IdempotencyRepository(session), _StubProviderUnpaidOnPoll()
    )

    order = await service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    await _expire_hold(session, order.id)

    released = await service.release_expired_holds()

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.CANCELLED.value
    assert released == 1
    assert await stock_of(products, "MILK-COW-1L") == 5


async def test_sweep_leaves_the_hold_in_place_when_the_gateway_is_unreachable(
    session, products, orders, user, milk
):
    """A gateway hiccup during reconciliation must never be treated as
    'unpaid' — that would cancel orders precisely when we are least sure."""
    from app.repositories.idempotency import IdempotencyRepository
    from app.services.order_service import OrderService

    service = OrderService(
        products, orders, IdempotencyRepository(session), _StubProviderGatewayUnreachable()
    )

    order = await service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    await _expire_hold(session, order.id)

    released = await service.release_expired_holds()

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.PENDING_PAYMENT.value  # untouched
    assert released == 0
    assert await stock_of(products, "MILK-COW-1L") == 3  # still held, not released


# ---------------------------------------------------------------------------
# AAD-PAY-006 / AAD-PAY-014 — expire_by and partial_payment on link creation
# ---------------------------------------------------------------------------


async def test_create_order_sets_expire_by_and_disables_partial_payment(provider, monkeypatch):
    captured: dict = {}

    def fake_create(body):
        captured.update(body)
        return {"id": "plink_new", "short_url": "https://rzp.io/l/abc"}

    monkeypatch.setattr(provider._client.payment_link, "create", fake_create)

    expires_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    await provider.create_order(
        amount_paise=10_000,
        currency="INR",
        receipt="ord_1",
        notes={},
        expires_at=expires_at,
    )

    assert captured["expire_by"] == int(expires_at.timestamp())
    assert captured["partial_payment"] is False


async def test_create_order_without_expires_at_omits_expire_by(provider, monkeypatch):
    """expires_at is optional on the interface (COD-only deployments, or a
    provider that doesn't need it) — must not send a malformed field."""
    captured: dict = {}

    def fake_create(body):
        captured.update(body)
        return {"id": "plink_new", "short_url": "https://rzp.io/l/abc"}

    monkeypatch.setattr(provider._client.payment_link, "create", fake_create)

    await provider.create_order(amount_paise=10_000, currency="INR", receipt="ord_1", notes={})

    assert "expire_by" not in captured
    assert captured["partial_payment"] is False


# ---------------------------------------------------------------------------
# RazorpayProvider.poll_status itself
# ---------------------------------------------------------------------------


class TestPollStatus:
    async def test_returns_none_when_not_paid(self, provider, monkeypatch):
        monkeypatch.setattr(
            provider._client.payment_link, "fetch", lambda _id: {"status": "created"}
        )
        assert await provider.poll_status(provider_order_id="plink_ABC") is None

    async def test_returns_a_capture_event_when_paid(self, provider, monkeypatch):
        monkeypatch.setattr(
            provider._client.payment_link,
            "fetch",
            lambda _id: {
                "id": "plink_ABC",
                "status": "paid",
                "reference_id": "ord_our_own_id",
                "amount_paid": 10_500,
                "payments": [{"payment_id": "pay_XYZ"}],
            },
        )
        event = await provider.poll_status(provider_order_id="plink_ABC")
        assert event is not None
        assert event.is_capture is True
        assert event.order_id == "ord_our_own_id"
        assert event.provider_payment_id == "pay_XYZ"
        assert event.amount_paise == 10_500

    async def test_gateway_error_is_wrapped_as_upstream_error(self, provider, monkeypatch):
        from app.core.errors import UpstreamError

        def boom(_id):
            raise RuntimeError("network blew up")

        monkeypatch.setattr(provider._client.payment_link, "fetch", boom)
        with pytest.raises(UpstreamError):
            await provider.poll_status(provider_order_id="plink_ABC")


async def test_mock_provider_poll_status_is_always_none():
    assert await MockPaymentProvider().poll_status(provider_order_id="mockord_1") is None
