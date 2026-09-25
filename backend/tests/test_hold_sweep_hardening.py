"""AAD-REL-005 — the hold sweeper used to select up to 100 abandoned
checkouts with no row lock at all (so two replicas raced the same 100 rows)
and process all of them in one transaction (so one order's failure rolled
back every earlier order the same pass had already cancelled). This file
covers the two mechanisms `OrderService.release_expired_holds` /
`OrderRepository.claim_expired_hold` now use to fix that: `FOR UPDATE SKIP
LOCKED` claiming, and a per-order SAVEPOINT.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Order as OrderRow
from app.domain.enums import OrderStatus
from app.repositories.orders import OrderRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.order_service import OrderService

ADDRESS = Address(label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034")


def _order_request(lines) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines], address=ADDRESS
    )


async def _expire_hold(session, order_id: str) -> None:
    await session.execute(
        update(OrderRow)
        .where(OrderRow.id == order_id)
        .values(hold_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.flush()


# ---------- claim_expired_hold: FOR UPDATE SKIP LOCKED ----------


async def test_list_expired_hold_ids_respects_the_limit(order_service, session, orders, user, milk):
    for _ in range(3):
        order = await order_service.create_order(
            user_id=user["id"], request=_order_request([("MILK-COW-1L", 1)]), idempotency_key=None
        )
        await _expire_hold(session, order.id)

    ids = await orders.list_expired_hold_ids(limit=2)
    assert len(ids) == 2


async def test_claim_expired_hold_returns_none_for_an_ineligible_order(
    orders, session, order_service, user, milk
):
    order = await order_service.create_order(
        user_id=user["id"], request=_order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    # Never expired — claim_expired_hold re-checks eligibility itself, not
    # just list_expired_hold_ids.
    assert await orders.claim_expired_hold(order.id) is None


async def test_claim_expired_hold_skips_a_row_locked_by_another_transaction(
    engine, order_service, session, user, milk
):
    """The direct AAD-REL-005 regression: simulates a second replica's sweep
    already having this row locked via `FOR UPDATE` in its own, still-open
    transaction — `claim_expired_hold`'s `SKIP LOCKED` must return `None`
    rather than blocking on it or double-processing it."""
    order = await order_service.create_order(
        user_id=user["id"], request=_order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await _expire_hold(session, order.id)
    await session.flush()
    await session.commit()  # the locking connection below needs to see this row

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    locking_session = factory()
    try:
        from sqlalchemy import select

        await locking_session.execute(
            select(OrderRow).where(OrderRow.id == order.id).with_for_update()
        )
        # locking_session now holds the row lock, uncommitted.

        claimed = await OrderRepository(session).claim_expired_hold(order.id)
        assert claimed is None, "a row locked elsewhere must be skipped, not blocked on"
    finally:
        await locking_session.rollback()
        await locking_session.close()


# ---------- release_expired_holds: per-order savepoint isolation ----------


async def test_one_orders_failure_does_not_undo_an_earlier_orders_cancellation(
    order_service, orders, products, session, user, milk
):
    """The direct AAD-REL-005 regression for the all-or-nothing behaviour:
    two expired holds in the same sweep, the second one fails partway
    through its own processing — the first order's cancellation (already
    committed to its own savepoint) must survive, and the second order's
    partial writes must be rolled back rather than left in a broken
    half-cancelled state."""
    good_order = await order_service.create_order(
        user_id=user["id"], request=_order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    bad_order = await order_service.create_order(
        user_id=user["id"], request=_order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await _expire_hold(session, good_order.id)
    await _expire_hold(session, bad_order.id)

    original_notify = OrderService._notify_customer

    async def failing_notify(self, order, new_status):
        if order["id"] == bad_order.id:
            raise RuntimeError("simulated bug — after the transition and stock release already ran")
        return await original_notify(self, order, new_status)

    OrderService._notify_customer = failing_notify
    try:
        released = await order_service.release_expired_holds()
    finally:
        OrderService._notify_customer = original_notify

    assert released == 1

    good = await orders.get(good_order.id)
    assert good["status"] == OrderStatus.CANCELLED.value, (
        "an earlier order's already-committed cancellation must survive a later order's failure"
    )

    bad = await orders.get(bad_order.id)
    assert bad["status"] == OrderStatus.PENDING_PAYMENT.value, (
        "the failed order's own transition must be rolled back by its savepoint, "
        "not left half-applied"
    )


async def test_a_failed_order_does_not_leave_stock_released_but_uncredited(
    order_service, orders, products, session, user, milk
):
    """A stronger version of the above, checked at the inventory level: if
    the savepoint rollback were broken, this order's stock would show as
    released (credited back) while the order itself still says
    pending_payment — a state nothing else in the system expects."""
    from sqlalchemy import select

    from app.db.models import Variant as VariantRow

    async def stock_of(sku: str) -> int:
        result = await session.execute(select(VariantRow.stock_qty).where(VariantRow.sku == sku))
        return result.scalars().one()

    before = await stock_of("MILK-COW-1L")

    order = await order_service.create_order(
        user_id=user["id"], request=_order_request([("MILK-COW-1L", 1)]), idempotency_key=None
    )
    await _expire_hold(session, order.id)
    held = await stock_of("MILK-COW-1L")
    assert held == before - 1

    async def failing_notify(self, o, new_status):
        raise RuntimeError("simulated bug")

    original_notify = OrderService._notify_customer
    OrderService._notify_customer = failing_notify
    try:
        released = await order_service.release_expired_holds()
    finally:
        OrderService._notify_customer = original_notify

    assert released == 0
    still_held = await stock_of("MILK-COW-1L")
    assert still_held == held, "stock must not be credited back for a cancel that rolled back"

    updated = await orders.get(order.id)
    assert updated["status"] == OrderStatus.PENDING_PAYMENT.value
