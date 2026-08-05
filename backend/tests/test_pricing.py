"""Pricing is pure, so it gets exhaustive tests. Everything customers are
charged flows through these functions."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.domain.enums import StockPolicy
from app.schemas.catalog import Product, Variant
from app.services.pricing import build_cart, compute_delivery_fee, price_line


def make_variant(**kw) -> Variant:
    defaults = dict(
        sku="MILK-COW-1L", label="1 litre", pack_value=1, pack_unit="l",
        price_paise=3500, stock_qty=10, max_per_order=10,
    )
    return Variant(**{**defaults, **kw})


def make_product(variants: list[Variant], **kw) -> Product:
    defaults = dict(
        id="prd_test", slug="test-milk", name="Test Milk", description="",
        category="milk", prep_minutes=20,
    )
    return Product(**{**defaults, **kw}, variants=variants)


def test_line_total_is_unit_price_times_quantity():
    v = make_variant(price_paise=3500)
    line = price_line(make_product([v]), v, 3)
    assert line.qty == 3
    assert line.line_total_paise == 10_500


def test_quantity_clamps_to_available_stock():
    v = make_variant(stock_qty=2)
    line = price_line(make_product([v]), v, 5)
    assert line.qty == 2
    assert line.adjusted_from_qty == 5


def test_quantity_clamps_to_max_per_order():
    v = make_variant(stock_qty=100, max_per_order=4)
    line = price_line(make_product([v]), v, 9)
    assert line.qty == 4


def test_out_of_stock_line_is_not_sellable():
    v = make_variant(stock_qty=0)
    line = price_line(make_product([v]), v, 1)
    assert line.qty == 0
    assert line.unavailable_reason == "out_of_stock"
    assert not line.is_sellable


def test_made_to_order_ignores_stock_count():
    v = make_variant(stock_qty=0, stock_policy=StockPolicy.MADE_TO_ORDER, max_per_order=6)
    line = price_line(make_product([v]), v, 3)
    assert line.qty == 3
    assert line.is_sellable


def test_inactive_variant_is_discontinued():
    v = make_variant(is_active=False)
    line = price_line(make_product([v]), v, 1)
    assert line.unavailable_reason == "discontinued"


@pytest.mark.parametrize(
    "subtotal,expected",
    [
        (0, 0),
        (10_000, settings.delivery_fee_paise),
        (settings.free_delivery_threshold_paise - 1, settings.delivery_fee_paise),
        (settings.free_delivery_threshold_paise, 0),
        (settings.free_delivery_threshold_paise + 5000, 0),
    ],
)
def test_delivery_fee_thresholds(subtotal, expected):
    assert compute_delivery_fee(subtotal) == expected


def test_eta_is_the_slowest_item_in_the_cart():
    milk = make_variant(sku="MILK-1L")
    pickle = make_variant(sku="PKL-250G", price_paise=14_500)
    milk_product = make_product([milk], prep_minutes=20)
    pickle_product = make_product(
        [pickle], id="prd_pkl", slug="pickle", name="Pickle",
        category="pickles", prep_minutes=45,
    )
    lines = [
        price_line(milk_product, milk, 1),
        price_line(pickle_product, pickle, 1),
    ]
    cart = build_cart(lines, {"MILK-1L": milk_product, "PKL-250G": pickle_product})
    assert cart.eta_minutes == 45


def test_unavailable_lines_do_not_contribute_to_the_total():
    good = make_variant(sku="GOOD", price_paise=5000, stock_qty=5)
    gone = make_variant(sku="GONE", price_paise=9900, stock_qty=0)
    product = make_product([good, gone])
    lines = [price_line(product, good, 2), price_line(product, gone, 1)]
    cart = build_cart(lines, {"GOOD": product, "GONE": product})
    assert cart.subtotal_paise == 10_000
    assert cart.has_adjustments is True


def test_totals_are_always_integers():
    v = make_variant(price_paise=3333)
    product = make_product([v])
    cart = build_cart([price_line(product, v, 3)], {v.sku: product})
    assert isinstance(cart.subtotal_paise, int)
    assert isinstance(cart.total_paise, int)
    assert cart.total_paise == cart.subtotal_paise + cart.delivery_fee_paise
