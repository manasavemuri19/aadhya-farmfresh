"""Server-authoritative pricing.

Every rupee the customer is charged is computed here, from catalog data, at the
moment of the request. Client-supplied prices are never read. Pricing is pure —
no I/O, no clock, no database — which makes it exhaustively unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.schemas.catalog import Product, Variant


@dataclass(frozen=True, slots=True)
class PricedLine:
    sku: str
    product_id: str
    product_name: str
    variant_label: str
    image_url: str
    qty: int
    unit_price_paise: int
    line_total_paise: int
    adjusted_from_qty: int | None = None
    unavailable_reason: str | None = None

    @property
    def is_sellable(self) -> bool:
        return self.qty > 0 and self.unavailable_reason is None


@dataclass(frozen=True, slots=True)
class PricedCart:
    lines: list[PricedLine]
    subtotal_paise: int
    delivery_fee_paise: int
    discount_paise: int
    total_paise: int
    eta_minutes: int
    meets_minimum: bool
    has_adjustments: bool


def price_line(product: Product, variant: Variant, requested_qty: int) -> PricedLine:
    """Price one cart line, clamping quantity to what can actually be sold."""
    base = {
        "sku": variant.sku,
        "product_id": product.id,
        "product_name": product.name,
        "variant_label": variant.label,
        "image_url": product.image_url,
        "unit_price_paise": variant.price_paise,
    }

    if not product.is_active or not variant.is_active:
        return PricedLine(
            **base, qty=0, line_total_paise=0,
            adjusted_from_qty=requested_qty, unavailable_reason="discontinued",
        )

    sellable = variant.sellable_qty()
    if sellable <= 0:
        return PricedLine(
            **base, qty=0, line_total_paise=0,
            adjusted_from_qty=requested_qty, unavailable_reason="out_of_stock",
        )

    qty = min(requested_qty, sellable)
    return PricedLine(
        **base,
        qty=qty,
        line_total_paise=variant.price_paise * qty,
        adjusted_from_qty=requested_qty if qty != requested_qty else None,
    )


def compute_delivery_fee(subtotal_paise: int) -> int:
    if subtotal_paise <= 0:
        return 0
    if subtotal_paise >= settings.free_delivery_threshold_paise:
        return 0
    return settings.delivery_fee_paise


def compute_eta_minutes(lines: list[PricedLine], products_by_sku: dict[str, Product]) -> int:
    """The order ships together, so the slowest item sets the promise."""
    prep_times = [
        products_by_sku[line.sku].prep_minutes
        for line in lines
        if line.is_sellable and line.sku in products_by_sku
    ]
    return max(prep_times) if prep_times else 0


def build_cart(lines: list[PricedLine], products_by_sku: dict[str, Product]) -> PricedCart:
    sellable = [line for line in lines if line.is_sellable]
    subtotal = sum(line.line_total_paise for line in sellable)
    delivery_fee = compute_delivery_fee(subtotal)
    discount = 0
    total = subtotal + delivery_fee - discount

    return PricedCart(
        lines=lines,
        subtotal_paise=subtotal,
        delivery_fee_paise=delivery_fee,
        discount_paise=discount,
        total_paise=total,
        eta_minutes=compute_eta_minutes(lines, products_by_sku),
        meets_minimum=subtotal >= settings.min_order_paise,
        has_adjustments=any(
            line.adjusted_from_qty is not None or line.unavailable_reason for line in lines
        ),
    )
