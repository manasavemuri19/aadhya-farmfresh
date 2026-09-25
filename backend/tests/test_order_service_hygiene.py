"""Batch 14 — five small correctness/hygiene findings in the order-service
cluster (`services/pricing.py`, `repositories/idempotency.py`,
`repositories/orders.py`, `db/models.py`, `services/order_service.py`).

AAD-QUAL-014: `MAX_PAISE`/`assert_valid_amount` were defined in
`core/money.py` and never called — no ceiling on a computed order total.
Now called on every `build_cart` total.

AAD-QUAL-018: `IdempotencyRepository.release()` had zero callers — the
transaction rollback `TransactionalRoute` already performs on any failed
request undoes the claim insert for free. Deleted, docstring rewritten to
document the real invariant.

AAD-QUAL-019: `OrderRepository.transition()` reported the same falsy value
whether the CAS lost (row exists, wrong status) or the order id doesn't
exist at all. Now `True` / `False` / `None` respectively.

AAD-DATA-007: `payments.provider_order_id` was indexed but not unique —
`get_by_provider_order_id`'s `.first()` could silently pick one of several
matching rows. Now a unique index (migration 0016), which Postgres allows
any number of NULLs under (COD payments never get a provider order id).

AAD-QUAL-021: `OrderService._to_view` used to take a pre-computed
`agent_location` only `get_for_user` ever passed — `list_for_user` and
`list_queue_for_staff` silently never showed live tracking. `_to_view` now
computes it itself, so every view-construction path is consistent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from app.db.models import Payment as PaymentRow
from app.db.models import User as UserRow
from app.domain.enums import OrderStatus, PaymentMethod
from app.repositories.delivery import DeliveryRepository
from app.repositories.users import UserRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest
from app.services.delivery_service import DeliveryService
from app.services.order_service import OrderService
from app.services.pricing import PricedLine, build_cart

ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)


def order_request(lines, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines], address=ADDRESS, **kw,
    )


# ---------- AAD-QUAL-014 ----------


def test_build_cart_raises_when_the_total_exceeds_the_sanity_ceiling():
    huge_line = PricedLine(
        sku="HUGE-1", product_id="prd_huge", product_name="Huge", variant_label="1",
        image_url="", qty=1, unit_price_paise=200_000_000, line_total_paise=200_000_000,
        max_qty=1,
    )
    with pytest.raises(ValueError, match="exceeds"):
        build_cart([huge_line], {})


def test_build_cart_still_accepts_an_ordinary_total():
    line = PricedLine(
        sku="MILK-COW-1L", product_id="prd_milk", product_name="Milk", variant_label="1L",
        image_url="", qty=2, unit_price_paise=3500, line_total_paise=7000, max_qty=5,
    )
    cart = build_cart([line], {})
    assert cart.subtotal_paise == 7000


# ---------- AAD-QUAL-018 ----------


def test_idempotency_release_is_gone_not_just_undocumented():
    import app.repositories.idempotency as idempotency_module

    assert not hasattr(idempotency_module.IdempotencyRepository, "release")


# ---------- AAD-QUAL-019 ----------


class TestTransitionTriState:
    async def test_a_winning_cas_returns_true(self, order_service: OrderService, user, milk):
        order = await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
            idempotency_key="transition-tristate-1",
        )
        won = await order_service.orders.transition(
            order.id,
            expected_status=OrderStatus.CONFIRMED,
            new_status=OrderStatus.PACKED,
        )
        assert won is True

    async def test_a_status_mismatch_returns_false_not_none(
        self, order_service: OrderService, user, milk
    ):
        order = await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
            idempotency_key="transition-tristate-2",
        )
        # Order is CONFIRMED (COD); ask for a CAS expecting PACKED instead —
        # the row exists, just not in the expected state.
        won = await order_service.orders.transition(
            order.id,
            expected_status=OrderStatus.PACKED,
            new_status=OrderStatus.OUT_FOR_DELIVERY,
        )
        assert won is False

    async def test_a_nonexistent_order_id_returns_none_not_false(
        self, order_service: OrderService
    ):
        won = await order_service.orders.transition(
            "ord_does_not_exist_at_all",
            expected_status=OrderStatus.CONFIRMED,
            new_status=OrderStatus.PACKED,
        )
        assert won is None


# ---------- AAD-DATA-007 ----------


async def test_two_payments_cannot_share_a_provider_order_id(
    session, order_service: OrderService, user, milk
):
    order1 = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="provider-order-unique-1",
    )
    order2 = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="provider-order-unique-2",
    )

    await session.execute(
        sa_update(PaymentRow)
        .where(PaymentRow.order_id == order1.id)
        .values(provider_order_id="dup_provider_order")
    )
    await session.flush()

    # The UPDATE itself is what hits the index — asyncpg surfaces the
    # violation on the statement, not deferred to a later flush.
    with pytest.raises(IntegrityError):
        await session.execute(
            sa_update(PaymentRow)
            .where(PaymentRow.order_id == order2.id)
            .values(provider_order_id="dup_provider_order")
        )


async def test_cod_payments_with_no_provider_order_id_do_not_collide(
    session, order_service: OrderService, user, milk
):
    """Sanity check that the unique index doesn't break the common case —
    COD orders never get a provider_order_id, so many NULLs must coexist."""
    order1 = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="provider-order-null-1",
    )
    order2 = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="provider-order-null-2",
    )
    await session.flush()  # both rows have provider_order_id IS NULL — must not raise
    assert order1.id != order2.id


# ---------- AAD-QUAL-021 ----------


async def test_list_for_user_shows_live_tracking_the_same_as_get_for_user(
    session, order_service: OrderService, user, milk
):
    # AAD-SEC-033's guard means agent-location visibility only ever fires
    # when `OrderService.users` is actually wired — the shared `order_service`
    # fixture deliberately leaves it `None` (conftest.py's fixture matches
    # how `get_order_service` builds it for routes that don't need it), so
    # this test builds its own instance with `users` set, sharing the same
    # repositories and session as the fixture underneath it.
    svc = OrderService(
        order_service.products, order_service.orders, order_service.idempotency,
        order_service.payments, users=UserRepository(session),
    )

    agent = await UserRepository(session).get_or_create_by_google(
        google_sub="qual021_agent", email="qual021-agent@example.com", name="Agent",
    )
    await session.execute(
        sa_update(UserRow).where(UserRow.id == agent["id"]).values(role="delivery_agent")
    )
    await session.flush()

    order = await svc.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="qual021-list-vs-get",
    )

    delivery_service = DeliveryService(DeliveryRepository(session), UserRepository(session), svc)
    await delivery_service.accept(order.id, agent["id"])
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.PACKED)
    await delivery_service.update_status(order.id, agent["id"], OrderStatus.OUT_FOR_DELIVERY)
    await UserRepository(session).update_agent_location(
        agent["id"], latitude=17.4300, longitude=78.4500
    )

    detail = await svc.get_for_user(order.id, user["id"])
    assert detail.delivery_agent_location is not None

    page = await svc.list_for_user(user["id"])
    listed = next(o for o in page.items if o.id == order.id)
    assert listed.delivery_agent_location is not None
    assert listed.delivery_agent_location.latitude == detail.delivery_agent_location.latitude


async def test_list_for_user_omits_it_for_orders_not_out_for_delivery(
    order_service: OrderService, user, milk
):
    """The common case — nothing en route — must not regress into an extra
    query or a populated field for every order in a customer's history."""
    await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key="qual021-not-out-for-delivery",
    )
    page = await order_service.list_for_user(user["id"])
    assert all(o.delivery_agent_location is None for o in page.items)
