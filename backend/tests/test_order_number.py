"""Regression tests for AAD-DATA-001.

`human_order_number()` picked six random digits with no collision check —
a 50% chance of a collision after roughly 1,180 orders (the birthday bound
for a million-value space), which at 100 orders/day is about twelve days,
and nothing in the insert path checked for it: `orders.order_number` is
`UNIQUE`, so a collision would have surfaced as a raw `IntegrityError`
bubbling out of checkout.

`OrderRepository.next_order_number` replaces it with a daily-scoped
sequence (`AD-YYMMDD-NNNN`) backed by one row per calendar date in
`order_number_counters`, reserved with `INSERT ... ON CONFLICT DO UPDATE
... RETURNING`. These tests prove the format, the sequential/no-repeat
behaviour within a day, the per-day reset, and — the property that
actually matters — that concurrent callers on separate connections never
receive the same number.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.enums import PaymentMethod
from app.repositories.orders import OrderRepository
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

ORDER_NUMBER_RE = re.compile(r"^AD-\d{6}-\d{4}$")

ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)


def order_request(lines, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines],
        address=ADDRESS,
        **kw,
    )


async def test_format_is_ad_yymmdd_nnnn(orders: OrderRepository):
    number = await orders.next_order_number(today=date(2026, 9, 14))
    assert ORDER_NUMBER_RE.match(number), number
    assert number == "AD-260914-0001"


async def test_sequence_increments_within_the_same_day(orders: OrderRepository):
    first = await orders.next_order_number(today=date(2026, 9, 14))
    second = await orders.next_order_number(today=date(2026, 9, 14))
    third = await orders.next_order_number(today=date(2026, 9, 14))
    assert [first, second, third] == [
        "AD-260914-0001",
        "AD-260914-0002",
        "AD-260914-0003",
    ]


async def test_each_calendar_date_gets_its_own_sequence(orders: OrderRepository):
    """A new day starts back at 0001 — the counter is scoped per row, not global."""
    day_one_a = await orders.next_order_number(today=date(2026, 9, 14))
    day_one_b = await orders.next_order_number(today=date(2026, 9, 14))
    day_two_a = await orders.next_order_number(today=date(2026, 9, 15))

    assert day_one_a == "AD-260914-0001"
    assert day_one_b == "AD-260914-0002"
    assert day_two_a == "AD-260915-0001"


async def test_defaults_to_todays_utc_date(orders: OrderRepository):
    number = await orders.next_order_number()
    assert number.startswith(f"AD-{date.today():%y%m%d}-")


async def _reserve_on_own_connection(engine, today: date) -> str:
    """One reservation in its own session, committed independently — mirrors
    the pattern in test_concurrency_real.py: real concurrency requires
    separate connections, since one shared session only ever serializes."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        number = await OrderRepository(session).next_order_number(today=today)
        await session.commit()
        return number


async def test_concurrent_reservations_never_collide(engine, session):
    """The property the whole fix exists for: fifty simultaneous callers,
    fifty distinct numbers, no gaps, no repeats."""
    await session.commit()  # nothing to make visible yet, but keeps parity with siblings

    today = date(2026, 9, 14)
    numbers = await asyncio.gather(
        *(_reserve_on_own_connection(engine, today) for _ in range(50))
    )

    assert len(set(numbers)) == 50, "every concurrent caller must get a unique number"
    assert sorted(numbers) == [f"AD-260914-{i:04d}" for i in range(1, 51)]


async def test_created_order_carries_a_well_formed_order_number(
    order_service, user, products, milk
):
    """End-to-end: the service wires next_order_number() into a real order
    instead of the removed human_order_number()."""
    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    assert ORDER_NUMBER_RE.match(order.order_number), order.order_number


async def test_stock_rejected_order_does_not_burn_a_number(
    order_service, user, products, milk
):
    """next_order_number() is called only after stock is confirmed reserved
    (see order_service.py), so an OutOfStock order must not advance the
    counter — otherwise every failed checkout attempt would create gaps."""
    from app.core.errors import OutOfStock

    with pytest.raises(OutOfStock):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 99)], payment_method=PaymentMethod.COD),
            idempotency_key=None,
        )

    order = await order_service.create_order(
        user_id=user["id"],
        request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
        idempotency_key=None,
    )
    assert order.order_number.endswith("-0001")
