"""AAD-PAY-005 — a capture webhook whose amount doesn't match the order
total used to be a `log.error` and nothing else: the money stayed captured
at the gateway, the payment row stayed `created` (so `_maybe_refund`,
AAD-PAY-003, never sees it), and the only record was one log line.

Now it's flagged on the payment row (`amount_mismatch`, with the amount the
gateway actually reported), a support ticket opens immediately so a human
sees it, and the periodic sweeper (`process_amount_mismatches`, same
deferred-gateway-call shape as `process_pending_refunds`) refunds exactly
the amount that was actually captured.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import SupportTicket
from app.domain.enums import OrderStatus, PaymentStatus
from app.payments.base import WebhookEvent
from app.repositories.support import SupportRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.order_service import OrderService

ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)


def order_request(lines, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines], address=ADDRESS, **kw
    )


async def _place_pending_order(order_service, orders, user, sku: str):
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([(sku, 1)]), idempotency_key=None
    )
    doc = await orders.get(order.id)
    assert doc["status"] == OrderStatus.PENDING_PAYMENT.value
    return order, doc


def _capture_event(order_id: str, doc, *, amount_paise: int) -> WebhookEvent:
    return WebhookEvent(
        event_id=f"evt_capture_{order_id}", event_type="payment.captured",
        provider_order_id=doc["payment"]["provider_order_id"],
        provider_payment_id=f"pay_{order_id}", amount_paise=amount_paise, raw={},
    )


async def test_amount_mismatch_flags_the_payment_with_the_received_amount(
    order_service, orders, user, milk
):
    order, doc = await _place_pending_order(order_service, orders, user, "MILK-COW-1L")
    wrong_amount = doc["payment"]["amount_paise"] - 500

    await order_service.apply_webhook(_capture_event(order.id, doc, amount_paise=wrong_amount))

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.PENDING_PAYMENT.value  # not silently confirmed
    assert updated["payment"]["status"] == PaymentStatus.AMOUNT_MISMATCH.value
    assert updated["payment"]["received_amount_paise"] == wrong_amount
    assert updated["payment"]["provider_payment_id"] == f"pay_{order.id}"


async def test_amount_mismatch_clears_the_hold_so_the_sweep_stops_repolling(
    order_service, orders, user, milk
):
    """Without this, `release_expired_holds` would find this order stale,
    poll the gateway, see the same mismatched amount, and re-flag/re-ticket
    it every sweep forever (AAD-PAY-005's own fix note)."""
    order, doc = await _place_pending_order(order_service, orders, user, "MILK-COW-1L")
    assert doc["hold_expires_at"] is not None

    await order_service.apply_webhook(
        _capture_event(order.id, doc, amount_paise=doc["payment"]["amount_paise"] - 500)
    )

    updated = await orders.get(order.id)
    assert updated["hold_expires_at"] is None


async def test_amount_mismatch_opens_a_support_ticket(session, products, orders, user, milk):
    from app.payments.mock import MockPaymentProvider
    from app.repositories.idempotency import IdempotencyRepository

    support = SupportRepository(session)
    service = OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider(), support=support
    )
    order, doc = await _place_pending_order(service, orders, user, "MILK-COW-1L")

    await service.apply_webhook(
        _capture_event(order.id, doc, amount_paise=doc["payment"]["amount_paise"] - 500)
    )

    tickets = (
        await session.execute(select(SupportTicket).where(SupportTicket.user_id == user["id"]))
    ).scalars().all()
    assert len(tickets) == 1
    assert tickets[0].context_node_id == "payment_amount_mismatch"
    assert order.id in tickets[0].message or order.order_number in tickets[0].message


async def test_duplicate_mismatch_event_does_not_re_flag_or_re_ticket(
    session, products, orders, user, milk
):
    from app.payments.mock import MockPaymentProvider
    from app.repositories.idempotency import IdempotencyRepository

    support = SupportRepository(session)
    service = OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider(), support=support
    )
    order, doc = await _place_pending_order(service, orders, user, "MILK-COW-1L")
    event = _capture_event(order.id, doc, amount_paise=doc["payment"]["amount_paise"] - 500)

    await service.apply_webhook(event)
    await service.apply_webhook(event)  # a re-poll of the same still-unresolved order

    tickets = (
        await session.execute(select(SupportTicket).where(SupportTicket.user_id == user["id"]))
    ).scalars().all()
    assert len(tickets) == 1


async def test_process_amount_mismatches_refunds_the_received_amount_not_the_order_total(
    order_service, orders, user, milk, monkeypatch
):
    order, doc = await _place_pending_order(order_service, orders, user, "MILK-COW-1L")
    received = doc["payment"]["amount_paise"] - 500
    await order_service.apply_webhook(_capture_event(order.id, doc, amount_paise=received))

    calls = []

    async def fake_refund(*, provider_payment_id, amount_paise, notes):
        calls.append((provider_payment_id, amount_paise, notes))
        return "rfnd_fake"

    monkeypatch.setattr(order_service.payments, "refund", fake_refund)

    refunded_count = await order_service.process_amount_mismatches()

    assert refunded_count == 1
    assert len(calls) == 1
    called_payment_id, called_amount, notes = calls[0]
    assert called_payment_id == f"pay_{order.id}"
    assert called_amount == received  # not doc["payment"]["amount_paise"]
    assert notes["reason"] == "amount_mismatch"

    updated = await orders.get(order.id)
    assert updated["payment"]["status"] == PaymentStatus.REFUNDED.value
    # The order itself is left for a human — still unconfirmed, not auto-cancelled.
    assert updated["status"] == OrderStatus.PENDING_PAYMENT.value


async def test_one_orders_mismatch_refund_failure_does_not_block_another(
    order_service, orders, user, milk, monkeypatch
):
    milk.variants[0].stock_qty = 5
    order_a, doc_a = await _place_pending_order(order_service, orders, user, "MILK-COW-1L")
    await order_service.apply_webhook(
        _capture_event(order_a.id, doc_a, amount_paise=doc_a["payment"]["amount_paise"] - 500)
    )
    order_b, doc_b = await _place_pending_order(order_service, orders, user, "MILK-COW-1L")
    await order_service.apply_webhook(
        _capture_event(order_b.id, doc_b, amount_paise=doc_b["payment"]["amount_paise"] - 500)
    )

    async def selective_refund(*, provider_payment_id, amount_paise, notes):
        if notes["order_id"] == order_a.id:
            raise Exception("gateway had a genuinely unexpected failure")
        return "rfnd_ok"

    monkeypatch.setattr(order_service.payments, "refund", selective_refund)

    refunded_count = await order_service.process_amount_mismatches()

    assert refunded_count == 1
    a_doc = await orders.get(order_a.id)
    b_doc = await orders.get(order_b.id)
    assert a_doc["payment"]["status"] == PaymentStatus.AMOUNT_MISMATCH.value  # retried next sweep
    assert b_doc["payment"]["status"] == PaymentStatus.REFUNDED.value


async def test_process_amount_mismatches_is_a_quiet_noop_with_nothing_flagged(order_service):
    assert await order_service.process_amount_mismatches() == 0
