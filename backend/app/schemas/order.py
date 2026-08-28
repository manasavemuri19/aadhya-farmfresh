"""Cart and order wire models.

The client posts SKUs and quantities — never prices. The server re-prices every
line from the catalog at checkout time. A client that sends a price is ignored,
which removes an entire class of tampering bug by construction.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

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


class CancelOrderRequest(Schema):
    reason: str = Field(default="", max_length=200)


class UpdateOrderStatusRequest(Schema):
    status: OrderStatus
    note: str = Field(default="", max_length=200)


class VerifyPaymentRequest(Schema):
    """Posted by the client after the gateway sheet closes. Treated as a hint
    only — the webhook is the source of truth for money."""

    order_id: str
    provider_order_id: str
    provider_payment_id: str
    signature: str


class AdjustStockRequest(Schema):
    sku: str
    # Absolute set, or a relative delta — one of the two, never both.
    set_qty: int | None = Field(default=None, ge=0)
    delta_qty: int | None = None
    reason: str = Field(default="manual_adjustment", max_length=64)


class SetPriceRequest(Schema):
    sku: str
    price_paise: int = Field(ge=0)
