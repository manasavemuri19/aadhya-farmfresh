"""Batch 18: sharing one Product object across its own SKUs in
find_variants, get_product's single-query id/slug branch, a narrower order
idempotency fingerprint, and idempotency support on admin stock deltas.
"""

from __future__ import annotations

import pytest

from app.api.deps import Principal
from app.api.v1.routes.admin import adjust_stock
from app.core.errors import Conflict, NotFound
from app.schemas.order import AdjustStockRequest, CartLineInput, CreateOrderRequest
from app.schemas.auth import Address
from app.services.catalog_service import CatalogService
from app.services.order_service import OrderService


ADDRESS = Address(
    label="Home", line1="12-3-45 Banjara Hills", city="Hyderabad", pincode="500034"
)


# --- AAD-PERF-011: find_variants shares one Product per distinct product ---


async def test_find_variants_shares_the_same_product_object_across_its_own_skus(
    products, milk
):
    result = await products.find_variants(["MILK-COW-1L", "MILK-COW-500ML"])
    product_1l, _ = result["MILK-COW-1L"]
    product_500ml, _ = result["MILK-COW-500ML"]
    # Both SKUs belong to the same underlying product row — the fix builds
    # that Product once and reuses it, rather than rebuilding it per SKU.
    assert product_1l is product_500ml


async def test_find_variants_still_returns_distinct_products_for_distinct_products(
    products, milk, khoya
):
    result = await products.find_variants(["MILK-COW-1L", "KHOYA-250G"])
    milk_product, _ = result["MILK-COW-1L"]
    khoya_product, _ = result["KHOYA-250G"]
    assert milk_product is not khoya_product
    assert milk_product.id != khoya_product.id


# --- AAD-PERF-011: get_product branches on the id prefix, one query either way ---


async def test_get_product_finds_by_prefixed_id(products, milk):
    svc = CatalogService(products)
    view = await svc.get_product(milk.id)
    assert view.id == milk.id
    assert view.slug == "full-cream-cow-milk"


async def test_get_product_finds_by_slug(products, milk):
    svc = CatalogService(products)
    view = await svc.get_product("full-cream-cow-milk")
    assert view.id == milk.id


async def test_get_product_404s_for_an_unknown_id(products, milk):
    svc = CatalogService(products)
    with pytest.raises(NotFound):
        await svc.get_product("prd_does_not_exist")


async def test_get_product_404s_for_an_unknown_slug(products, milk):
    svc = CatalogService(products)
    with pytest.raises(NotFound):
        await svc.get_product("no-such-slug")


# --- AAD-QUAL-017: the idempotency fingerprint covers lines/payment/total only ---


def _request(lines, **kw) -> CreateOrderRequest:
    return CreateOrderRequest(
        lines=[CartLineInput(sku=s, qty=q) for s, q in lines], address=ADDRESS, **kw
    )


def test_fingerprint_is_stable_when_only_notes_or_address_differ():
    from app.domain.enums import PaymentMethod

    base = _request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD)
    retried = _request(
        [("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD, notes="leave at the gate"
    )
    assert OrderService._fingerprint(base) == OrderService._fingerprint(retried)


def test_fingerprint_is_stable_regardless_of_line_order():
    from app.domain.enums import PaymentMethod

    a = _request([("MILK-COW-1L", 3), ("KHOYA-250G", 1)], payment_method=PaymentMethod.COD)
    b = _request([("KHOYA-250G", 1), ("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD)
    assert OrderService._fingerprint(a) == OrderService._fingerprint(b)


def test_fingerprint_changes_when_quantity_changes():
    from app.domain.enums import PaymentMethod

    a = _request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD)
    b = _request([("MILK-COW-1L", 4)], payment_method=PaymentMethod.COD)
    assert OrderService._fingerprint(a) != OrderService._fingerprint(b)


def test_fingerprint_changes_when_payment_method_changes():
    from app.domain.enums import PaymentMethod

    a = _request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.COD)
    b = _request([("MILK-COW-1L", 3)], payment_method=PaymentMethod.ONLINE)
    assert OrderService._fingerprint(a) != OrderService._fingerprint(b)


# --- AAD-SEC-026: idempotency on delta_qty admin stock adjustments --------


async def test_delta_adjust_stock_with_a_repeated_key_applies_the_delta_only_once(
    session, products, milk, idempotency
):
    body = AdjustStockRequest(sku="MILK-COW-1L", delta_qty=-2, reason="breakage")
    staff = Principal("usr_staff1", "staff")

    first = await adjust_stock(body, staff, products, idempotency, idempotency_key="stk-0001")
    second = await adjust_stock(body, staff, products, idempotency, idempotency_key="stk-0001")

    assert first == second
    # MILK-COW-1L starts at 5 (conftest's `milk` fixture) — a single -2
    # delta should land at 3, not 1 (which is what a double-apply would do).
    assert await stock_of(products, "MILK-COW-1L") == 3


async def test_delta_adjust_stock_with_the_same_key_but_a_different_delta_conflicts(
    session, products, milk, idempotency
):
    staff = Principal("usr_staff1", "staff")
    await adjust_stock(
        AdjustStockRequest(sku="MILK-COW-1L", delta_qty=-2, reason="breakage"),
        staff, products, idempotency, idempotency_key="stk-0002",
    )
    with pytest.raises(Conflict):
        await adjust_stock(
            AdjustStockRequest(sku="MILK-COW-1L", delta_qty=-3, reason="breakage"),
            staff, products, idempotency, idempotency_key="stk-0002",
        )


async def test_delta_adjust_stock_without_a_key_still_applies_every_call(
    session, products, milk, idempotency
):
    """No Idempotency-Key sent (the existing staff client doesn't send one
    today) — behaviour is unchanged from before AAD-SEC-026: every call
    applies, there's nothing to opt into."""
    body = AdjustStockRequest(sku="MILK-COW-1L", delta_qty=-1, reason="breakage")
    staff = Principal("usr_staff1", "staff")
    await adjust_stock(body, staff, products, idempotency)
    await adjust_stock(body, staff, products, idempotency)
    assert await stock_of(products, "MILK-COW-1L") == 3


async def stock_of(products, sku: str) -> int:
    from sqlalchemy import select

    from app.db.models import Variant as VariantRow

    result = await products.session.execute(
        select(VariantRow.stock_qty).where(VariantRow.sku == sku)
    )
    return result.scalars().one()
