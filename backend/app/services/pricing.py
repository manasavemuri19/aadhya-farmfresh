"""Server-authoritative pricing.

Every rupee the customer is charged is computed here, from catalog data, at the
moment of the request. Client-supplied prices are never read.

AAD-QUAL-016: this used to claim pricing is "pure — no I/O, no clock, no
database". `compute_delivery_fee` and `build_cart` both read the global
`settings` object (`free_delivery_threshold_paise`, `delivery_fee_paise`,
`min_order_paise`) — a real, if quiet, dependency: a config change between
a quote and the checkout that follows it can make the two disagree, and any
test exercising these functions has to hold `settings` fixed (or patch it)
rather than being able to treat every input as an explicit argument. Every
function here is still deterministic given its inputs *and* the current
`settings` — no clock, no database, no network — which is what actually
makes this exhaustively unit-testable; "pure" overstated it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.core.money import assert_valid_amount
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
    # AAD-QUAL-013: how many of this SKU can be sold right now
    # (`variant.sellable_qty()`), always populated — not just when a clamp
    # happened — so a client can enforce the real ceiling in its own
    # quantity stepper instead of discovering it only after a rejected
    # checkout. 0 for a fully unavailable line.
    max_qty: int = 0
    # AAD-QUAL-013: *why* qty was clamped below what was requested, when it
    # was — distinct from unavailable_reason, which only ever describes a
    # line that is not sellable at all (qty == 0). A line can be clamped and
    # still fully sellable at the reduced qty (e.g. asking for 12 of an
    # item capped at 10 per order, with 200 in stock) — that case sets this
    # field, not unavailable_reason, so is_sellable below is unaffected by
    # it. "out_of_stock": genuinely fewer than requested remain in stock.
    # "quantity_limit": stock is fine; the per-order cap is what bound.
    adjustment_reason: str | None = None

    @property
    def is_sellable(self) -> bool:
        return self.qty > 0 and self.unavailable_reason is None


@dataclass(frozen=True, slots=True)
class PricedCart:
    lines: list[PricedLine]
    subtotal_paise: int
    delivery_fee_paise: int
    total_paise: int
    eta_minutes: int
    meets_minimum: bool
    has_adjustments: bool


def price_line(product: Product, variant: Variant, requested_qty: int) -> PricedLine:
    """Price one cart line, clamping quantity to what can actually be sold.

    Fields are assigned explicitly at each return rather than splatted from a
    shared `**base` dict (the previous shape): a plain `dict[str, object]`
    can't be checked precisely against a dataclass constructor, which is
    exactly the pattern AAD-SEC-021 flags elsewhere in this codebase for the
    same reason. Explicit kwargs let mypy verify every field at every
    branch, including the two AAD-QUAL-013 adds below.
    """
    sku = variant.sku
    product_id = product.id
    product_name = product.name
    variant_label = variant.label
    image_url = product.image_url
    unit_price_paise = variant.price_paise

    if not product.is_active or not variant.is_active:
        return PricedLine(
            sku=sku, product_id=product_id, product_name=product_name,
            variant_label=variant_label, image_url=image_url,
            unit_price_paise=unit_price_paise, qty=0, line_total_paise=0,
            adjusted_from_qty=requested_qty, unavailable_reason="discontinued",
        )

    sellable = variant.sellable_qty()
    if sellable <= 0:
        return PricedLine(
            sku=sku, product_id=product_id, product_name=product_name,
            variant_label=variant_label, image_url=image_url,
            unit_price_paise=unit_price_paise, qty=0, line_total_paise=0,
            adjusted_from_qty=requested_qty, unavailable_reason="out_of_stock",
        )

    qty = min(requested_qty, sellable)
    if qty == requested_qty:
        return PricedLine(
            sku=sku, product_id=product_id, product_name=product_name,
            variant_label=variant_label, image_url=image_url,
            unit_price_paise=unit_price_paise, qty=qty,
            line_total_paise=unit_price_paise * qty, max_qty=sellable,
        )

    # Clamped, but still sellable at the reduced qty — AAD-QUAL-013: figure
    # out *which* constraint actually bound, so the customer is told the
    # true reason instead of a blanket "ran out". If stock itself is below
    # the per-order cap, stock is the honest explanation even though the
    # cap also technically applies; only when stock comfortably clears the
    # cap is the cap the sole reason the request was reduced.
    reason = "out_of_stock" if variant.stock_qty < variant.max_per_order else "quantity_limit"
    return PricedLine(
        sku=sku, product_id=product_id, product_name=product_name,
        variant_label=variant_label, image_url=image_url,
        unit_price_paise=unit_price_paise, qty=qty,
        line_total_paise=unit_price_paise * qty,
        adjusted_from_qty=requested_qty,
        adjustment_reason=reason,
        max_qty=sellable,
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
    # AAD-QUAL-015: a `discount` term used to sit here, hardcoded to `0` —
    # this app has no promotions system, so it could never be anything
    # else. Removed along with `PricedCart.discount_paise` and the matching
    # field everywhere it was threaded through (schemas, order_service,
    # OrderRepository, the `orders.discount_paise` DB column). If a
    # promotion feature is ever built, add the field back next to the code
    # that actually computes a real discount.
    total = subtotal + delivery_fee

    # AAD-QUAL-014: `MAX_PAISE`/`assert_valid_amount` were defined in
    # `core/money.py` and never called anywhere — the only guard on an order
    # total was the database's `CheckConstraint("total_paise >= 0")`, which
    # bounds it below but not above. Checked here, on every computed total,
    # because every cart quote and every order creation both go through
    # `build_cart` — there's no second place a total gets computed that
    # would need its own check. ₹10 lakh (`MAX_PAISE`) is far beyond any
    # real cart this catalog could produce, so hitting it means a genuine
    # bug (a runaway `qty`, a corrupted price) rather than a large but
    # legitimate order — the same "should be impossible" territory as this
    # batch's other invariant checks, which is why this raises rather than
    # returning a clamped or client-facing-validation value.
    assert_valid_amount(total, field="total_paise")

    return PricedCart(
        lines=lines,
        subtotal_paise=subtotal,
        delivery_fee_paise=delivery_fee,
        total_paise=total,
        eta_minutes=compute_eta_minutes(lines, products_by_sku),
        meets_minimum=subtotal >= settings.min_order_paise,
        has_adjustments=any(
            line.adjusted_from_qty is not None or line.unavailable_reason for line in lines
        ),
    )
