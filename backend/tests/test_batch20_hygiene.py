"""Batch 20: reserve_stock_bulk (AAD-PERF-008) and the query-count
regression test AAD-OPS-025 asked for.
"""

from __future__ import annotations

from app.domain.enums import PaymentMethod
from app.schemas.auth import Address
from app.schemas.order import CartLineInput, CreateOrderRequest

ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)


def order_request(lines, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines], address=ADDRESS, **kw
    )


# --- AAD-PERF-008: reserve_stock_bulk ------------------------------------


async def test_reserve_stock_bulk_reserves_every_line_that_fits(products, milk):
    result = await products.reserve_stock_bulk(
        [("MILK-COW-1L", 2), ("MILK-COW-500ML", 0)]
    )
    assert result == {"MILK-COW-1L": True, "MILK-COW-500ML": True}


async def test_reserve_stock_bulk_reports_insufficient_stock_for_one_line_only(
    products, milk
):
    # MILK-COW-1L starts at 5 (conftest's milk fixture).
    result = await products.reserve_stock_bulk([("MILK-COW-1L", 999)])
    assert result == {"MILK-COW-1L": False}


async def test_reserve_stock_bulk_mixes_success_and_failure_across_lines(products, milk):
    result = await products.reserve_stock_bulk(
        [("MILK-COW-1L", 2), ("MILK-COW-500ML", 999)]
    )
    assert result == {"MILK-COW-1L": True, "MILK-COW-500ML": False}


async def test_reserve_stock_bulk_treats_made_to_order_as_ok_with_no_decrement(
    products, khoya, session
):
    from sqlalchemy import select

    from app.db.models import Variant as VariantRow

    result = await products.reserve_stock_bulk([("KHOYA-250G", 3)])
    assert result == {"KHOYA-250G": True}
    row = (
        await session.execute(
            select(VariantRow.stock_qty).where(VariantRow.sku == "KHOYA-250G")
        )
    ).scalar_one()
    assert row == 0  # unchanged — made-to-order never decrements


async def test_reserve_stock_bulk_empty_input_returns_empty(products):
    assert await products.reserve_stock_bulk([]) == {}


async def test_create_order_with_a_race_lost_between_pricing_and_reservation_still_names_the_sku(
    order_service, user, milk, session, engine, monkeypatch
):
    """End-to-end reproduction of the one scenario reserve_stock_bulk (or
    the old per-line reserve_stock) actually guards against: stock looked
    fine when this order was priced, but a concurrent buyer took the last
    unit before this order's own reservation ran. The bulk reservation still
    surfaces the same "X just sold out" naming the specific line, not a
    generic all-or-nothing failure.

    Discovered while verifying this in Batch 21: the first version of this
    test committed the racer's drain *before* calling `create_order` at
    all, relying on `order_service`'s session still holding the `milk`
    fixture's own in-memory `VariantRow` (stock_qty=5, ORM-loaded via
    `upsert_product`'s `session.add()`) to make pricing see the pre-race
    number while only `reserve_stock_bulk`'s live compare-and-swap saw the
    drained one. That's not actually the race this method guards against —
    it's an accident of SQLAlchemy's identity-map merge behavior for an
    object already loaded earlier in the *same* session, which is not
    deterministic (confirmed directly: ~20% of runs saw a freshly-merged,
    already-drained value instead, and pricing correctly rejected the whole
    cart before reservation ever ran — "Nothing in your cart is available",
    not the SKU-naming message this test asserts). It also doesn't match a
    real request at all: a real request's session has never seen this
    variant row before `_price()` runs, so it would always read live data,
    with no identity-map staleness possible either way.

    Fixed by constructing the actual race explicitly instead of relying on
    it to fall out of session reuse: `OrderService._price` is monkeypatched
    to run the real pricing call first (a live read — this order genuinely
    sees stock=5 and prices the line as available), *then* drain the stock
    via a second, real, committed session — the same setup
    `test_concurrency_real.py` uses — before returning pricing's result.
    That reproduces the literal sequence the docstring describes: priced
    successfully, then lost the unit before reservation, deterministically,
    every time.
    """
    import pytest
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.errors import OutOfStock
    from app.repositories.products import ProductRepository
    from app.services.order_service import OrderService

    # Make the `milk` fixture's data visible to the separate connection
    # below (same pattern test_concurrency_real.py uses) — otherwise it's
    # racing against data nothing outside this session can see yet.
    await session.commit()

    real_price = OrderService._price

    async def _price_then_drain(self, lines):
        result = await real_price(self, lines)
        # MILK-COW-1L starts at 5 (conftest's milk fixture). Pricing above
        # has already run and already sees it as available — draining it
        # now, in a separate, already-committed session, simulates a buyer
        # who takes the last unit strictly between this order's pricing and
        # its reservation.
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as racer_session:
            await ProductRepository(racer_session).reserve_stock("MILK-COW-1L", 5)
            await racer_session.commit()
        return result

    monkeypatch.setattr(OrderService, "_price", _price_then_drain)

    with pytest.raises(OutOfStock, match="1 litre"):
        await order_service.create_order(
            user_id=user["id"],
            request=order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD),
            idempotency_key="batch20-race-fail",
        )


# --- AAD-OPS-025: query count doesn't scale with cart size ---------------


async def test_create_order_query_count_does_not_scale_with_cart_size(
    order_service, user, milk, khoya, query_counter
):
    """Before AAD-PERF-008, reserve_stock's serial per-line loop meant a
    bigger cart issued strictly more queries just to reserve stock — one
    to two extra round trips per extra line. reserve_stock_bulk does the
    reservation step in a fixed number of round trips regardless of cart
    size, so a 1-line and a 3-line order shouldn't differ by much; what's
    left (pricing/catalog lookups) already batches through find_variants.
    MILK-COW-500ML is avoided here — the `milk` fixture starts it at zero
    stock, which would fail at pricing, before reservation is ever reached.
    """
    small = order_request([("MILK-COW-1L", 1)], payment_method=PaymentMethod.COD)
    await order_service.create_order(
        user_id=user["id"], request=small, idempotency_key="batch20-qc-small"
    )
    small_count = query_counter.count

    query_counter.count = 0
    big = order_request(
        [("MILK-COW-1L", 1), ("KHOYA-250G", 1)],
        payment_method=PaymentMethod.COD,
    )
    await order_service.create_order(
        user_id=user["id"], request=big, idempotency_key="batch20-qc-big"
    )
    big_count = query_counter.count

    assert big_count - small_count <= 3


# --- AAD-DATA-013: a database-level updated_at trigger --------------------


async def test_updated_at_trigger_fires_even_for_a_raw_sql_update(session, user):
    """`0020_updated_at_trigger` isn't exercised by the ordinary test setup
    at all — `conftest.py`'s `engine` fixture builds the schema straight
    from the SQLAlchemy models (`Base.metadata.create_all`), not by
    running the Alembic migration chain, so a trigger that only exists as
    migration DDL is invisible to every other test in this suite. This
    test installs the same trigger the migration creates directly (on
    `users` only — one table is enough to prove the mechanism), then
    proves the actual point of AAD-DATA-013: a raw SQL UPDATE that never
    goes through SQLAlchemy's `onupdate` at all still bumps `updated_at`,
    because the guarantee now lives in the database, not in a Python
    keyword argument a future write path could simply not use.
    """
    import asyncio

    from sqlalchemy import text

    await session.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
            BEGIN
                NEW.updated_at = now();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TRIGGER trg_users_updated_at
            BEFORE UPDATE ON users
            FOR EACH ROW
            EXECUTE FUNCTION set_updated_at();
            """
        )
    )
    await session.commit()

    before = (
        await session.execute(
            text("SELECT updated_at FROM users WHERE id = :id"), {"id": user["id"]}
        )
    ).scalar_one()

    # A real clock tick, so a timestamp comparison below can't pass by
    # coincidence (two `now()` calls in the same statement can land in the
    # same microsecond on a fast test database).
    await asyncio.sleep(0.05)

    # Raw SQL, deliberately not through OrderRepository/UserRepository or
    # any SQLAlchemy update() construct — this is exactly the "psql fix,
    # admin script, future text() query" write path the finding named.
    await session.execute(
        text("UPDATE users SET name = :name WHERE id = :id"),
        {"name": "Renamed By Raw SQL", "id": user["id"]},
    )
    await session.commit()

    after = (
        await session.execute(
            text("SELECT updated_at FROM users WHERE id = :id"), {"id": user["id"]}
        )
    ).scalar_one()

    assert after > before
