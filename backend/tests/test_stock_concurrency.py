"""The reservation invariant: stock can never be oversold, and never leaks."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Variant as VariantRow
from app.repositories.products import ProductRepository


async def _stock(products: ProductRepository, sku: str) -> int:
    result = await products.session.execute(
        select(VariantRow.stock_qty).where(VariantRow.sku == sku)
    )
    return result.scalars().one()


async def _price(products: ProductRepository, sku: str) -> int:
    result = await products.session.execute(
        select(VariantRow.price_paise).where(VariantRow.sku == sku)
    )
    return result.scalars().one()


async def test_set_price_updates_the_variant(products, milk):
    assert await products.set_price("MILK-COW-1L", 3800) is True
    assert await _price(products, "MILK-COW-1L") == 3800


async def test_set_price_on_unknown_sku_returns_false(products, milk):
    assert await products.set_price("DOES-NOT-EXIST", 1000) is False


async def test_reserve_decrements_stock(products, milk):
    assert await products.reserve_stock("MILK-COW-1L", 2) is True
    assert await _stock(products, "MILK-COW-1L") == 3


async def test_reserve_fails_when_requesting_more_than_available(products, milk):
    assert await products.reserve_stock("MILK-COW-1L", 6) is False
    # A failed reservation must leave stock untouched.
    assert await _stock(products, "MILK-COW-1L") == 5


async def test_reserve_fails_on_zero_stock(products, milk):
    assert await products.reserve_stock("MILK-COW-500ML", 1) is False


async def test_made_to_order_never_blocks_and_never_decrements(products, khoya):
    assert await products.reserve_stock("KHOYA-250G", 4) is True
    assert await _stock(products, "KHOYA-250G") == 0


async def test_sequential_reservations_exhaust_then_refuse(products, milk):
    """Five litres available: five reservations succeed, the sixth is refused."""
    results = [await products.reserve_stock("MILK-COW-1L", 1) for _ in range(6)]
    assert results == [True, True, True, True, True, False]
    assert await _stock(products, "MILK-COW-1L") == 0


async def test_release_returns_stock_to_the_shelf(products, milk):
    await products.reserve_stock("MILK-COW-1L", 3)
    await products.release_stock("MILK-COW-1L", 3)
    assert await _stock(products, "MILK-COW-1L") == 5


async def test_adjust_stock_cannot_go_negative(products, milk):
    assert await products.adjust_stock("MILK-COW-1L", -10) is False
    assert await _stock(products, "MILK-COW-1L") == 5
