"""True concurrency, across separate database connections.

Every other test in this suite shares one session, so its "concurrency" is
sequential. These tests open independent connections and run them at the same
time, which is the only way to prove the reservation logic actually holds
under the condition it exists for: two customers tapping "Place order" on the
last litre of milk in the same instant.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Variant as VariantRow
from app.repositories.products import ProductRepository


async def _reserve_on_own_connection(engine, sku: str, qty: int) -> bool:
    """One reservation in its own session, committed independently."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        ok = await ProductRepository(session).reserve_stock(sku, qty)
        await session.commit()
        return ok


async def _stock(engine, sku: str) -> int:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            select(VariantRow.stock_qty).where(VariantRow.sku == sku)
        )
        return result.scalars().one()


async def test_ten_concurrent_buyers_cannot_oversell_five_litres(engine, session, milk):
    """The invariant this whole system is built around."""
    await session.commit()   # make the fixture data visible to other connections

    results = await asyncio.gather(
        *(_reserve_on_own_connection(engine, "MILK-COW-1L", 1) for _ in range(10))
    )

    assert sum(results) == 5, "exactly five of ten buyers should win"
    assert await _stock(engine, "MILK-COW-1L") == 0


async def test_concurrent_multi_unit_reservations_do_not_oversell(engine, session, milk):
    """Four buyers each want two litres; only five exist. Two can be served."""
    await session.commit()

    results = await asyncio.gather(
        *(_reserve_on_own_connection(engine, "MILK-COW-1L", 2) for _ in range(4))
    )

    assert sum(results) == 2
    assert await _stock(engine, "MILK-COW-1L") == 1


async def test_database_constraint_rejects_negative_stock(engine, session, milk):
    """Belt and braces: even a direct UPDATE cannot drive stock below zero.

    This is the guarantee that survives a future bug in application code.
    """
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    await session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as bad_session:
        with pytest.raises(IntegrityError):
            await bad_session.execute(
                update(VariantRow)
                .where(VariantRow.sku == "MILK-COW-1L")
                .values(stock_qty=-1)
            )
            await bad_session.commit()


async def test_concurrent_reservations_of_made_to_order_all_succeed(
    engine, session, khoya
):
    await session.commit()
    results = await asyncio.gather(
        *(_reserve_on_own_connection(engine, "KHOYA-250G", 1) for _ in range(6))
    )
    assert all(results)
