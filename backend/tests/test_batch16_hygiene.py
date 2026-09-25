"""Batch 16: a typed response for POST /auth/google, the dead discount_paise
field, adjust_stock's ambiguous False and its missing stock_policy filter,
and a visibility improvement for upsert_product's rename gap.
"""

from __future__ import annotations

import logging

import pytest

from app.api.deps import Principal
from app.api.v1.routes.admin import adjust_stock
from app.api.v1.routes.auth import google_sign_in
from app.core.errors import NotFound, ValidationError
from app.schemas.auth import GoogleSignInRequest, GoogleSignInResponse, TokenPair, UserProfile
from app.schemas.order import AdjustStockRequest


# --- AAD-API-001: POST /auth/google returns a typed model -----------------


class _FakeAuthService:
    async def verify_google_and_login(self, id_token: str):
        return (
            TokenPair(access_token="a", refresh_token="r", expires_in=900),
            UserProfile(id="usr_1", email="x@example.com", name="X"),
        )


async def test_google_sign_in_returns_a_typed_response_not_a_bare_dict():
    result = await google_sign_in(GoogleSignInRequest(id_token="fake"), _FakeAuthService())
    assert isinstance(result, GoogleSignInResponse)
    assert result.tokens.access_token == "a"
    assert result.user.id == "usr_1"


# --- AAD-QUAL-015: discount_paise is gone, not just always zero -----------


def test_priced_cart_has_no_discount_field():
    from app.services.pricing import PricedCart

    assert "discount_paise" not in PricedCart.__dataclass_fields__


def test_order_view_and_quote_schemas_have_no_discount_field():
    from app.schemas.order import OrderView, Quote

    assert "discount_paise" not in Quote.model_fields
    assert "discount_paise" not in OrderView.model_fields


# --- AAD-API-005 / AAD-QUAL-030: adjust_stock's three distinct outcomes ---


async def test_adjust_stock_reports_no_such_sku_distinctly(products):
    ok, reason = await products.adjust_stock("DOES-NOT-EXIST", -1)
    assert ok is False
    assert reason == "no_such_sku"


async def test_adjust_stock_reports_insufficient_stock_distinctly(products, milk):
    ok, reason = await products.adjust_stock("MILK-COW-1L", -999)
    assert ok is False
    assert reason == "insufficient_stock"


async def test_adjust_stock_reports_not_tracked_for_a_made_to_order_variant(
    products, khoya
):
    ok, reason = await products.adjust_stock("KHOYA-250G", 1)
    assert ok is False
    assert reason == "not_tracked"


async def test_adjust_stock_still_succeeds_for_an_ordinary_tracked_variant(products, milk):
    ok, reason = await products.adjust_stock("MILK-COW-1L", -1)
    assert ok is True
    assert reason is None


async def test_adjust_stock_route_reports_404_for_a_missing_sku(products, idempotency):
    with pytest.raises(NotFound):
        await adjust_stock(
            AdjustStockRequest(sku="GHOST-SKU", delta_qty=-1, reason="test"),
            Principal("usr_staff1", "staff"),
            products,
            idempotency,
        )


async def test_adjust_stock_route_reports_a_clear_message_for_a_made_to_order_variant(
    products, khoya, idempotency
):
    with pytest.raises(ValidationError, match="isn't stock-tracked"):
        await adjust_stock(
            AdjustStockRequest(sku="KHOYA-250G", delta_qty=1, reason="test"),
            Principal("usr_staff1", "staff"),
            products,
            idempotency,
        )


async def test_adjust_stock_route_still_reports_negative_stock_distinctly(
    products, milk, idempotency
):
    with pytest.raises(ValidationError, match="below zero"):
        await adjust_stock(
            AdjustStockRequest(sku="MILK-COW-1L", delta_qty=-999, reason="test"),
            Principal("usr_staff1", "staff"),
            products,
            idempotency,
        )


# --- AAD-DATA-018: a slug-mismatch insert is now visible -------------------


async def test_upsert_product_logs_a_warning_when_no_existing_row_matches_the_slug(
    products, categories, caplog
):
    from app.core.ids import new_id
    from app.schemas.catalog import Product, Variant

    product = Product(
        id=new_id("prd", 12), slug="brand-new-item", name="New", category="milk",
        variants=[
            Variant(
                sku="NEW-SKU-1", label="1", pack_value=1, pack_unit="piece",
                price_paise=1000, stock_qty=5,
            )
        ],
    )
    with caplog.at_level(logging.WARNING):
        await products.upsert_product(product)
    assert any("inserting a new product" in r.message for r in caplog.records)


async def test_upsert_product_does_not_warn_when_updating_an_existing_slug(
    products, milk, caplog
):
    from app.schemas.catalog import Product, Variant

    updated = Product(
        id=milk.id, slug=milk.slug, name="Milk (updated)", category=milk.category,
        variants=[
            Variant(
                sku=v.sku, label=v.label, pack_value=v.pack_value, pack_unit=v.pack_unit,
                price_paise=v.price_paise, stock_qty=v.stock_qty,
            )
            for v in milk.variants
        ],
    )
    with caplog.at_level(logging.WARNING):
        await products.upsert_product(updated)
    assert not any("inserting a new product" in r.message for r in caplog.records)
