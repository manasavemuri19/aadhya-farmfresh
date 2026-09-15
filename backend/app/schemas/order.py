"""Cart and order wire models.

The client posts SKUs and quantities — never prices. The server re-prices every
line from the catalog at checkout time. A client that sends a price is ignored,
which removes an entire class of tampering bug by construction.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from app.core.money import MAX_PAISE
from app.domain.enums import OrderStatus, PaymentMethod, PaymentStatus
from app.schemas.auth import Address
from app.schemas.common import Schema


class CartLineInput(Schema):
    sku: str = Field(max_length=48)
    qty: int = Field(ge=1, le=99)


class CartInput(Schema):
    lines: list[CartLineInput] = Field(min_length=1, max_length=50)


class QuoteLine(Schema):
    sku: str
    product_id: str
    product_name: str
    variant_label: str
    image_url: str
    qty: int
    unit_price_paise: int
    line_total_paise: int
    # Populated when the requested quantity had to be reduced or dropped.
    adjusted_from_qty: int | None = None
    unavailable_reason: str | None = None


class Quote(Schema):
    """A priced, availability-checked cart. Cheap to call; safe to poll."""

    lines: list[QuoteLine]
    subtotal_paise: int
    delivery_fee_paise: int
    discount_paise: int = 0
    total_paise: int
    currency: str = "INR"
    free_delivery_threshold_paise: int
    min_order_paise: int
    meets_minimum: bool
    eta_minutes: int
    has_adjustments: bool = False


class CreateOrderRequest(Schema):
    lines: list[CartLineInput] = Field(min_length=1, max_length=50)
    address: Address
    payment_method: PaymentMethod = PaymentMethod.ONLINE
    notes: str = Field(default="", max_length=280)
    # The client's own total, in paise. If the server's recomputed total differs,
    # the request is rejected rather than silently charging a different amount.
    expected_total_paise: int | None = Field(default=None, ge=0)


class PaymentView(Schema):
    method: PaymentMethod
    status: PaymentStatus
    amount_paise: int
    provider: str | None = None
    provider_order_id: str | None = None
    # Handed to the client SDK to open the checkout sheet.
    checkout_payload: dict | None = None


class OrderLine(Schema):
    sku: str
    product_id: str
    product_name: str
    variant_label: str
    image_url: str
    qty: int
    unit_price_paise: int
    line_total_paise: int


class StatusEvent(Schema):
    status: OrderStatus
    at: datetime
    note: str = ""
    by: str = "system"


class AgentLocation(Schema):
    latitude: float
    longitude: float
    updated_at: datetime


class OrderView(Schema):
    id: str
    order_number: str
    status: OrderStatus
    lines: list[OrderLine]
    subtotal_paise: int
    delivery_fee_paise: int
    discount_paise: int
    total_paise: int
    currency: str
    address: Address
    notes: str
    payment: PaymentView
    eta_minutes: int
    timeline: list[StatusEvent]
    created_at: datetime
    updated_at: datetime
    can_cancel: bool = False
    # Same window as can_cancel (CUSTOMER_CANCELLABLE) — once an order is
    # packed for pickup, changing its destination needs a person, not a form.
    can_edit_address: bool = False
    # Only ever populated while status == out_for_delivery AND an agent is
    # assigned — see OrderService._agent_location_if_visible. Deliberately
    # never shown before pickup or after drop-off: there's nothing useful
    # (or appropriate) to say about an agent's location outside that window.
    delivery_agent_location: AgentLocation | None = None


class CancelOrderRequest(Schema):
    reason: str = Field(default="", max_length=200)


class UpdateOrderStatusRequest(Schema):
    status: OrderStatus
    note: str = Field(default="", max_length=200)


class UpdateOrderAddressRequest(Schema):
    address: Address


# AAD-API-006: a ceiling on any single stock quantity or adjustment — far
# above any realistic single-SKU inventory for a farm shop, but bounded, so
# a stray extra zero sets thousands of units rather than a billion.
MAX_STOCK_QTY = 100_000


class AdjustStockRequest(Schema):
    sku: str
    # Absolute set, or a relative delta — one of the two, never both.
    set_qty: int | None = Field(default=None, ge=0, le=MAX_STOCK_QTY)
    delta_qty: int | None = Field(default=None, ge=-MAX_STOCK_QTY, le=MAX_STOCK_QTY)
    reason: str = Field(default="manual_adjustment", max_length=64)
    # AAD-DATA-016 / AAD-DATA-015: required alongside set_qty — the value
    # the staff screen was showing when it loaded. `set_stock` is a
    # compare-and-swap keyed on this, so a screen that's gone stale (someone
    # else adjusted stock, or an order reserved some, since the screen
    # loaded) is refused with a 409 rather than silently overwriting — and
    # because the swap is now guaranteed accurate, the stock-ledger delta
    # computed from it (new - expected) is finally the real delta, not the
    # absolute value the ledger used to record verbatim.
    expected_qty: int | None = Field(default=None, ge=0, le=MAX_STOCK_QTY)


class SetPriceRequest(Schema):
    sku: str
    price_paise: int = Field(ge=0, le=MAX_PAISE)
    # AAD-DATA-009: optional — omit it to change price only and leave the
    # stored MRP untouched. Send it alongside price_paise to also raise (or
    # clear) the MRP in the same call, which is what lets a staff member
    # raise a price past the variant's *current* MRP without a separate,
    # racy two-call dance.
    mrp_paise: int | None = Field(default=None, ge=0, le=MAX_PAISE)
    # AAD-API-006: a price change past ±50% of the *currently stored* price
    # needs this set explicitly — checked at the repository boundary, where
    # the current price is actually known. Catches a stray extra zero
    # ("₹50" typed as "₹5000") without blocking a genuine repricing — and
    # also catches the audit's own example (price_paise=0 making a product
    # free), since dropping to 0 is always a >50% decrease. See set_price.
    confirm_large_change: bool = False

    @model_validator(mode="after")
    def _check_discount(self) -> SetPriceRequest:
        # Only catches the case where both are sent together and disagree.
        # mrp_paise omitted (left as whatever's already stored) can still
        # end up inconsistent with a raised price_paise — that's caught at
        # the repository boundary, where the actual stored MRP is known.
        if self.mrp_paise is not None and self.mrp_paise < self.price_paise:
            raise ValueError("mrp_paise must be at least price_paise")
        return self
