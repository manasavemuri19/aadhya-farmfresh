"""Batch 24: AAD-QUAL-023 — two different event-id schemes for the same
logical payment.

Investigated rather than patched. The finding as originally written
described *three* schemes (`linkcb_<payment_id>` from the old hand-built
callback event, plus the real webhook's own id, plus `poll_<link_id>` from
`poll_status`) and asked for them to be unified, ideally all derived from the
gateway's own payment id. That premise is partly stale: `AAD-PAY-010`
(fixed earlier than this finding was last revisited) already replaced the
`linkcb_` scheme entirely — `/link-callback` no longer builds its own event
by hand, it calls the same `poll_status` the sweep uses, so there are only
two schemes left, not three: the real webhook's own `event_id` (Razorpay's
opaque per-delivery id, read from `X-Razorpay-Event-Id`, or a body hash as a
fallback for a provider that sends neither), and `poll_<payment_link_id>`
(the sweep, and `/link-callback`, when polling the gateway directly).

Full unification turns out not to be achievable the way the finding
suggested, for a structural reason worth writing down rather than
rediscovering later: Razorpay's own webhook event id is opaque and assigned
by Razorpay per delivery attempt — it has no relationship to the payment id
or the payment link id, and nothing on our side can derive one from the
other. So a real webhook delivery and a poll of the same underlying payment
will always produce two different `event_id` values, however this code is
written. Deduping by `(provider, event_id)` in `webhook_events` was never
actually the mechanism protecting against double-processing across
*channels* (webhook vs. poll) — it only protects against the same channel
redelivering its own identical event, which gateways do routinely. The
mechanism that protects cross-channel duplicates is `apply_webhook`'s own
idempotency: `OrderRepository.transition`'s compare-and-swap on the order's
*current* status, freshly read at the top of every call.

These tests pin that directly, simulating the realistic case this finding
actually worried about: the real webhook and a poll both eventually deliver
"this payment captured" for the same order, under two different event ids,
in either order. Each delivery gets its own fresh, separately-committed
session/`OrderService` — deliberately, not two calls sharing one session.
Two real webhook deliveries are two separate HTTP requests in production,
each with its own fresh session that has never seen this order's row
before, so it always reads live data with no identity map involved at all.
An earlier draft of this file called `apply_webhook` twice against the same
shared session instead, and hit exactly the non-deterministic SQLAlchemy
identity-map merge behavior this session's own `AAD-OPS-025`/Batch 20 race
test investigation already found and documented for an ORM-inserted row
re-selected later in the *same* session — it does not reflect anything a
real second delivery could actually trigger, only an artifact of the test's
own (wrong) shape. Fixed by giving the second delivery its own session, the
same fix that investigation's race test needed for the same reason.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.enums import OrderStatus, PaymentStatus
from app.payments.base import WebhookEvent
from app.payments.mock import MockPaymentProvider
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.orders import OrderRepository
from app.repositories.products import ProductRepository
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


def _capture_event(event_id: str, *, provider_order_id: str, amount_paise: int) -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id,
        event_type="payment_link.paid",
        provider_order_id=provider_order_id,
        provider_payment_id="pay_shared_across_channels",
        amount_paise=amount_paise,
        raw={},
    )


async def _deliver_in_a_fresh_session(engine, event: WebhookEvent) -> None:
    """Simulates a second, independent webhook/poll delivery — its own HTTP
    request in production, so its own fresh session that has never seen
    this order before. This is what makes the two deliveries' idempotency
    actually be enforced by the order's *committed* status (the real
    mechanism), not by whatever either delivery happens to still be holding
    in an in-process identity map."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as second_session:
        orders = OrderRepository(second_session)
        first_time = await orders.record_webhook_once(
            "razorpay", event.event_id, {"source": event.event_id}
        )
        assert first_time is True  # a genuinely different event id, not a replay
        svc = OrderService(
            ProductRepository(second_session),
            orders,
            IdempotencyRepository(second_session),
            MockPaymentProvider(),
        )
        await svc.apply_webhook(event)
        await second_session.commit()


async def test_the_two_schemes_really_do_still_produce_different_event_ids():
    """Not a bug — a fact about the design this finding needs to hold for
    its own reasoning to apply. If this ever stopped being true (Razorpay
    started sending a payment-id-derived event id, say), the rest of this
    file's premise — and the docstring above — would need revisiting."""
    webhook_event = _capture_event(
        "evt_real_webhook_abc123", provider_order_id="plink_XYZ", amount_paise=3500
    )
    poll_event = _capture_event(
        "poll_plink_XYZ", provider_order_id="plink_XYZ", amount_paise=3500
    )
    assert webhook_event.event_id != poll_event.event_id
    assert webhook_event.provider_payment_id == poll_event.provider_payment_id


async def test_webhook_then_poll_for_the_same_capture_confirms_the_order_exactly_once(
    order_service, orders, session, engine, user, milk, monkeypatch
):
    notify_calls = []
    notify_agent_calls = []
    real_notify = OrderService._notify_customer
    real_notify_agents = OrderService._notify_agents_new_order

    async def counting_notify(self, order, new_status):
        notify_calls.append(new_status)
        return await real_notify(self, order, new_status)

    async def counting_notify_agents(self, order):
        notify_agent_calls.append(order["id"])
        return await real_notify_agents(self, order)

    monkeypatch.setattr(OrderService, "_notify_customer", counting_notify)
    monkeypatch.setattr(OrderService, "_notify_agents_new_order", counting_notify_agents)

    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await session.commit()
    doc = await orders.get(order.id)
    assert doc["status"] == OrderStatus.PENDING_PAYMENT.value
    provider_order_id = doc["payment"]["provider_order_id"]

    # The real webhook arrives first, with its own opaque event id.
    await _deliver_in_a_fresh_session(
        engine,
        _capture_event(
            "evt_real_webhook_abc123",
            provider_order_id=provider_order_id,
            amount_paise=order.total_paise,
        ),
    )
    confirmed = await orders.get(order.id)
    assert confirmed["status"] == OrderStatus.CONFIRMED.value

    # Then the sweep's poll_status also reports the same payment captured —
    # a different event id, a different (fresh) session, so `record_webhook_
    # once` correctly sees "first time" for *that* id and `apply_webhook`
    # runs again, exactly as it would in production.
    await _deliver_in_a_fresh_session(
        engine,
        _capture_event(
            "poll_plink_XYZ", provider_order_id=provider_order_id, amount_paise=order.total_paise
        ),
    )

    # The order itself is unaffected by the second delivery — still
    # CONFIRMED, not re-transitioned or duplicated in the timeline.
    still_confirmed = await orders.get(order.id)
    assert still_confirmed["status"] == OrderStatus.CONFIRMED.value
    confirmed_events = [
        e for e in still_confirmed["timeline"] if e["status"] == OrderStatus.CONFIRMED.value
    ]
    assert len(confirmed_events) == 1

    # And the side effects that actually matter — notifying the customer and
    # the delivery pool — fired exactly once, from the CAS in `transition`
    # winning only the first time, not from the event-id dedup (which let
    # both calls through).
    assert notify_calls == [OrderStatus.CONFIRMED]
    assert len(notify_agent_calls) == 1


async def test_poll_then_webhook_order_is_equally_safe(
    order_service, orders, session, engine, user, milk
):
    """The reverse delivery order — the sweep's poll happens to win the
    race and the real webhook arrives after. Same guarantee, opposite
    arrival order, since nothing here depends on which channel goes
    first."""
    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await session.commit()
    doc = await orders.get(order.id)
    provider_order_id = doc["payment"]["provider_order_id"]

    await _deliver_in_a_fresh_session(
        engine,
        _capture_event(
            "poll_plink_XYZ", provider_order_id=provider_order_id, amount_paise=order.total_paise
        ),
    )
    assert (await orders.get(order.id))["status"] == OrderStatus.CONFIRMED.value

    await _deliver_in_a_fresh_session(
        engine,
        _capture_event(
            "evt_real_webhook_abc123",
            provider_order_id=provider_order_id,
            amount_paise=order.total_paise,
        ),
    )
    final = await orders.get(order.id)
    assert final["status"] == OrderStatus.CONFIRMED.value
    assert len([e for e in final["timeline"] if e["status"] == OrderStatus.CONFIRMED.value]) == 1


async def test_late_capture_after_cancel_refunds_once_even_when_reported_by_both_channels(
    order_service, session, orders, engine, user, milk
):
    """The other place two channels can both fire for the same real-world
    event: AAD-PAY-001's backstop. If the webhook reports a capture on a
    now-cancelled order and refunds it automatically, a subsequent poll
    reporting the exact same (now-stale) capture must not restock, refund,
    or write a second timeline entry a second time."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select, update

    from app.db.models import Order as OrderRow
    from app.db.models import Variant as VariantRow

    order = await order_service.create_order(
        user_id=user["id"], request=order_request([("MILK-COW-1L", 2)]), idempotency_key=None
    )
    await session.execute(
        update(OrderRow)
        .where(OrderRow.id == order.id)
        .values(hold_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.flush()
    released = await order_service.release_expired_holds()
    assert released == 1
    await session.commit()
    cancelled = await orders.get(order.id)
    assert cancelled["status"] == OrderStatus.CANCELLED.value
    provider_order_id = cancelled["payment"]["provider_order_id"]

    async def stock_of(sku: str) -> int:
        result = await session.execute(select(VariantRow.stock_qty).where(VariantRow.sku == sku))
        return result.scalars().one()

    assert await stock_of("MILK-COW-1L") == 5  # released once, by the sweep

    # Webhook reports the late capture first — refunds automatically.
    await _deliver_in_a_fresh_session(
        engine,
        _capture_event(
            "evt_late_webhook", provider_order_id=provider_order_id, amount_paise=order.total_paise
        ),
    )
    refunded = await orders.get(order.id)
    assert refunded["status"] == OrderStatus.REFUNDED.value
    assert refunded["payment"]["status"] == PaymentStatus.REFUND_PENDING.value
    assert await stock_of("MILK-COW-1L") == 5  # not credited a second time

    # The sweep's own poll later reports the same (already-stale) capture.
    await _deliver_in_a_fresh_session(
        engine,
        _capture_event(
            "poll_late_arrival",
            provider_order_id=provider_order_id,
            amount_paise=order.total_paise,
        ),
    )

    final = await orders.get(order.id)
    assert final["status"] == OrderStatus.REFUNDED.value
    assert final["payment"]["status"] == PaymentStatus.REFUND_PENDING.value
    assert await stock_of("MILK-COW-1L") == 5  # still not double-credited
    refund_events = [e for e in final["timeline"] if e["status"] == OrderStatus.REFUNDED.value]
    assert len(refund_events) == 1  # one refund transition, not two
