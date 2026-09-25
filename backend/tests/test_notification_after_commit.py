"""AAD-REL-004 — integration: OrderService's push notifications are wired
through `defer_until_commit` (see order_service.py's `_notify_customer` /
`_notify_agents_new_order`), so they must not fire until the surrounding
batch is drained, and never at all if it isn't — proving the wiring, not
just the mechanism `test_outbox.py` already covers in isolation.
"""

from __future__ import annotations

from sqlalchemy import update as sa_update

from app.core import outbox
from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod, Role
from app.payments.mock import MockPaymentProvider
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.users import UserRepository
from app.services.order_service import OrderService
from tests.test_order_flow import order_request


class _FakePush:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    async def notify_users(self, user_ids, *, title, body, data=None) -> None:
        self.calls.append((list(user_ids), title))


async def test_agent_notification_on_a_cod_order_is_deferred_and_drained(
    session, products, orders, milk, user
):
    """A COD order confirms instantly and notifies delivery agents from
    inside `_create_order_inner` — the call this finding's own repro
    (`order_service.py:105-135`) is anchored on."""
    users = UserRepository(session)
    agent = await users.get_or_create_by_google(
        google_sub="agent_for_outbox_test", email="agent_outbox@example.com", name="Agent",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == agent["id"]).values(role=Role.DELIVERY_AGENT.value)
    )
    await session.flush()

    push = _FakePush()
    svc = OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider(),
        users=users, push=push,
    )

    token = outbox.start_batch()
    try:
        order = await svc.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
            idempotency_key=None,
        )
        assert order.status is OrderStatus.CONFIRMED
        assert push.calls == [], "must not have sent before the batch drained"

        await outbox.drain()
        assert push.calls, "should have queued an agent notification"
        assert push.calls[0][0] == [agent["id"]]
    finally:
        outbox.end_batch(token)


async def test_agent_notification_never_fires_if_the_batch_is_never_drained(
    session, products, orders, milk, user
):
    """The direct stand-in for "the commit failed" — a batch that opens,
    queues, and ends without draining, exactly what both commit boundaries
    do on a rollback."""
    users = UserRepository(session)
    agent = await users.get_or_create_by_google(
        google_sub="agent_for_outbox_test_2", email="agent_outbox2@example.com", name="Agent",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == agent["id"]).values(role=Role.DELIVERY_AGENT.value)
    )
    await session.flush()

    push = _FakePush()
    svc = OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider(),
        users=users, push=push,
    )

    token = outbox.start_batch()
    try:
        order = await svc.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
            idempotency_key=None,
        )
        assert order.status is OrderStatus.CONFIRMED
    finally:
        outbox.end_batch(token)  # deliberately never drained

    assert push.calls == [], "a push must never fire for a transaction that never committed"


async def test_customer_notification_on_cancel_is_deferred_and_drained(
    session, products, orders, milk, user
):
    """`_cancel` calls `_notify_customer` — a different call site from the
    creation path above, proving the wiring isn't accidentally scoped to
    just one of the two notification methods."""
    push = _FakePush()
    svc = OrderService(
        products, orders, IdempotencyRepository(session), MockPaymentProvider(), push=push,
    )

    token = outbox.start_batch()
    try:
        order = await svc.create_order(
            user_id=user["id"], request=order_request([("MILK-COW-1L", 1)]),
            idempotency_key=None,
        )
        assert push.calls == []

        await svc.cancel(order_id=order.id, user_id=user["id"], reason="changed my mind")
        assert push.calls == [], "must not have sent before this batch drained"

        await outbox.drain()
        assert push.calls, "should have queued a cancellation notification"
        assert push.calls[0][0] == [user["id"]]
    finally:
        outbox.end_batch(token)
